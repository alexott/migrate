import json
import os, re, time

from databricks_cli.sdk import ApiClient, ClusterService, InstancePoolService, DbfsService

from databricks_migrate import log
from databricks_migrate.migrations import BaseMigrationClient


class ClusterMigrations(BaseMigrationClient):
    create_configs = {'num_workers',
                      'autoscale',
                      'cluster_name',
                      'spark_version',
                      'spark_conf',
                      'aws_attributes',
                      'node_type_id',
                      'driver_node_type_id',
                      'ssh_public_keys',
                      'custom_tags',
                      'cluster_log_conf',
                      'init_scripts',
                      'spark_env_vars',
                      'autotermination_minutes',
                      'enable_elastic_disk',
                      'instance_pool_id',
                      'pinned_by_user_name',
                      'creator_user_name',
                      'cluster_id'}

    def __init__(self, api_client: ApiClient, api_client_v1_2: ApiClient, export_dir, is_aws, skip_failed, verify_ssl):
        super().__init__(api_client, api_client_v1_2, export_dir, is_aws, skip_failed, verify_ssl)
        self.clusters_service = ClusterService(api_client)
        self.instance_pool_service = InstancePoolService(api_client)
        self.dbfs_service = DbfsService(api_client)

    def get_spark_versions(self):
        return self.clusters_service.list_spark_versions()

    def get_cluster_list(self, alive=True):
        """
        Returns an array of json objects for the running clusters.
        Grab the cluster_name or cluster_id
        """
        cl = self.clusters_service.list_clusters()
        if alive:
            running = filter(lambda x: x['state'] == "RUNNING", cl['clusters'])
            return list(running)
        else:
            clusters_list = cl.get('clusters', None)
            return clusters_list if clusters_list else []

    def remove_automated_clusters(self, cluster_list, log_file='skipped_clusters.log'):
        """
        Automated clusters like job clusters or model endpoints should be excluded
        :param cluster_list: list of cluster configurations
        :return: cleaned list with automated clusters removed
        """
        # model endpoint clusters start with the following
        ml_model_pattern = "mlflow-model-"
        # job clusters have specific format, job-JOBID-run-RUNID
        re_expr = re.compile("job-\d+-run-\d+$")
        clean_cluster_list = []
        with open(self._export_dir + log_file, 'w') as log_fp:
            for cluster in cluster_list:
                cluster_name = cluster['cluster_name']
                if re_expr.match(cluster_name) or cluster_name.startswith(ml_model_pattern):
                    log_fp.write(json.dumps(cluster) + '\n')
                else:
                    clean_cluster_list.append(cluster)
        return clean_cluster_list

    def log_cluster_configs(self, log_file='clusters.log'):
        """
        Log the current cluster configs in json file
        :param log_file:
        :return:
        """
        cluster_log = self._export_dir + log_file
        # pinned by cluster_user is a flag per cluster
        cl_raw = self.get_cluster_list(False)
        cl = self.remove_automated_clusters(cl_raw)
        # No instance profiles service
        ips = self.api_client.perform_query("GET", '/instance-profiles/list').get('instance_profiles', None)
        if ips:
            # filter none if we hit a profile w/ a none object
            # generate list of registered instance profiles to check cluster configs against
            ip_list = list(filter(None, [x.get('instance_profile_arn', None) for x in ips]))

        # filter on these items as MVP of the cluster configs
        # https://docs.databricks.com/api/latest/clusters.html#request-structure
        with open(cluster_log, "w") as log_fp:
            for x in cl:
                run_properties = set(list(x.keys())) - self.create_configs
                for p in run_properties:
                    del x[p]
                cluster_json = x
                if 'aws_attributes' in cluster_json:
                    aws_conf = cluster_json.pop('aws_attributes')
                    iam_role = aws_conf.get('instance_profile_arn', None)
                    if iam_role:
                        if (iam_role not in ip_list):
                            log.info("Skipping log of default IAM role: " + iam_role)
                            del aws_conf['instance_profile_arn']
                            cluster_json['aws_attributes'] = aws_conf
                    cluster_json['aws_attributes'] = aws_conf
                log_fp.write(json.dumps(cluster_json) + '\n')

    def cleanup_cluster_pool_configs(self, cluster_json, cluster_creator):
        """
        Pass in cluster json and cluster_creator to update fields that are not needed for clusters submitted to pools
        :param cluster_json:
        :param cluster_creator:
        :return:
        """
        pool_id_dict = self.get_instance_pool_id_mapping()
        # if pool id exists, remove instance types
        cluster_json.pop('node_type_id')
        cluster_json.pop('driver_node_type_id')
        cluster_json.pop('enable_elastic_disk')
        # add custom tag for original cluster creator for cost tracking
        if 'custom_tags' in cluster_json:
            tags = cluster_json['custom_tags']
            tags['OriginalCreator'] = cluster_creator
            cluster_json['custom_tags'] = tags
        else:
            cluster_json['custom_tags'] = {'OriginalCreator': cluster_creator}
        # remove all aws_attr except for IAM role if it exists
        if 'aws_attributes' in cluster_json:
            aws_conf = cluster_json.pop('aws_attributes')
            iam_role = aws_conf.get('instance_profile_arn', None)
            if not iam_role:
                cluster_json['aws_attributes'] = {'instance_profile_arn': iam_role}
        # map old pool ids to new pool ids
        old_pool_id = cluster_json['instance_pool_id']
        cluster_json['instance_pool_id'] = pool_id_dict[old_pool_id]
        return cluster_json

    def import_cluster_configs(self, log_file='clusters.log'):
        """
        Import cluster configs and update appropriate properties / tags in the new env
        :param log_file:
        :return:
        """
        cluster_log = self._export_dir + log_file
        if not os.path.exists(cluster_log):
            log.info("No clusters to import.")
            return
        current_cluster_names = set([x.get('cluster_name', None) for x in self.get_cluster_list(False)])
        # get instance pool id mappings
        with open(cluster_log, 'r') as fp:
            for line in fp:
                cluster_conf = json.loads(line)
                cluster_name = cluster_conf['cluster_name']
                if cluster_name in current_cluster_names:
                    log.info("Cluster already exists, skipping: {0}".format(cluster_name))
                    continue
                cluster_creator = cluster_conf.pop('creator_user_name')
                # check for instance pools and modify cluster attributes
                if 'instance_pool_id' in cluster_conf:
                    new_cluster_conf = self.cleanup_cluster_pool_configs(cluster_conf, cluster_creator)
                else:
                    # update cluster configs for non-pool clusters
                    # add original creator tag to help with DBU tracking
                    if 'custom_tags' in cluster_conf:
                        tags = cluster_conf['custom_tags']
                        tags['OriginalCreator'] = cluster_creator
                        cluster_conf['custom_tags'] = tags
                    else:
                        cluster_conf['custom_tags'] = {'OriginalCreator': cluster_creator}
                    new_cluster_conf = cluster_conf
                log.info("Creating cluster: {0}".format(new_cluster_conf['cluster_name']))
                # cluster_resp = '/clusters/create', new_cluster_conf)
                cluster_resp = self.clusters_service.create_cluster(**new_cluster_conf)
                if cluster_resp['http_status_code'] == 200:
                    stop_resp = self.clusters_service.delete_cluster(cluster_resp['cluster_id'])
                    if 'pinned_by_user_name' in cluster_conf:
                        pin_resp = self.clusters_service.pin_cluster(cluster_resp['cluster_id'])
                else:
                    log.info(cluster_resp)

    def delete_all_clusters(self):
        cl = self.get_cluster_list(False)
        for x in cl:
            self.clusters_service.unpin_cluster(x['cluster_id'])
            self.clusters_service.permanent_delete_cluster(x['cluster_id'])

    def log_instance_profiles(self, log_file='instance_profiles.log'):
        ip_log = self._export_dir + log_file
        ips = self.api_client.perform_query("GET", '/instance-profiles/list').get('instance_profiles', None)
        if ips:
            with open(ip_log, "w") as fp:
                for x in ips:
                    fp.write(json.dumps(x) + '\n')

    def import_instance_profiles(self, log_file='instance_profiles.log'):
        # currently an AWS only operation
        ip_log = self._export_dir + log_file
        if not os.path.exists(ip_log):
            log.info("No instance profiles to import.")
            return
        # check current profiles and skip if the profile already exists
        ip_list = self.api_client.perform_query("GET", '/instance-profiles/list').get('instance_profiles', None)
        if ip_list:
            list_of_profiles = [x['instance_profile_arn'] for x in ip_list]
        else:
            list_of_profiles = []
        with open(ip_log, "r") as fp:
            for line in fp:
                ip_arn = json.loads(line).get('instance_profile_arn', None)
                if ip_arn not in list_of_profiles:
                    log.info("Importing arn: {0}".format(ip_arn))
                    resp = self.api_client.perform_query("POST", '/instance-profiles/add',
                                                         data={'instance_profile_arn': ip_arn})
                else:
                    log.info("Skipping since profile exists: {0}".format(ip_arn))

    def log_instance_pools(self, log_file='instance_pools.log'):
        pool_log = self._export_dir + log_file
        pools = self.instance_pool_service.list_instance_pools().get('instance_pools', None)
        if pools:
            with open(pool_log, "w") as fp:
                for x in pools:
                    fp.write(json.dumps(x) + '\n')

    def import_instance_pools(self, log_file='instance_pools.log'):
        pool_log = self._export_dir + log_file
        if not os.path.exists(pool_log):
            log.info("No instance pools to import.")
            return
        with open(pool_log, 'r') as fp:
            for line in fp:
                pool_conf = json.loads(line)
                pool_resp = self.instance_pool_service.create_instance_pool(**pool_conf)

    def get_instance_pool_id_mapping(self, log_file='instance_pools.log'):
        pool_log = self._export_dir + log_file
        current_pools = self.instance_pool_service.list_instance_pools().get('instance_pools', None)
        if not current_pools:
            return None
        new_pools = {}
        # build dict of pool name and id mapping
        for p in current_pools:
            new_pools[p['instance_pool_name']] = p['instance_pool_id']
        # mapping id from old_pool_id to new_pool_id
        pool_mapping_dict = {}
        with open(pool_log, 'r') as fp:
            for line in fp:
                pool_conf = json.loads(line)
                old_pool_id = pool_conf['instance_pool_id']
                pool_name = pool_conf['instance_pool_name']
                new_pool_id = new_pools[pool_name]
                pool_mapping_dict[old_pool_id] = new_pool_id
        return pool_mapping_dict

    def get_global_init_scripts(self):
        """ return a list of global init scripts. Currently not logged """
        ls = self.dbfs_service.list('/databricks/init/').get('files', None)
        if ls is None:
            return []
        else:
            global_scripts = [{'path': x['path']} for x in ls if x['is_dir'] == False]
            return global_scripts

    def wait_for_cluster(self, cid):
        c_state = self.clusters_service.get_cluster(cid)
        while c_state['state'] != 'RUNNING':
            c_state = self.clusters_service.get_cluster(cid)
            log.info('Cluster state: {0}'.format(c_state['state']))
            time.sleep(2)
        return cid

    def get_cluster_id_by_name(self, cname):
        cl = self.clusters_service.list_clusters()
        running = list(filter(lambda x: x['state'] == "RUNNING", cl['clusters']))
        for x in running:
            if cname == x['cluster_name']:
                return x['cluster_id']
        return None

    def launch_cluster(self, iam_role=None):
        """ Launches a cluster to get DDL statements.
        Returns a cluster_id """
        # removed for now as Spark 3.0 will have backwards incompatible changes
        # version = self.get_latest_spark_version()
        import os
        real_path = os.path.dirname(os.path.realpath(__file__))
        if self.is_aws():
            with open(real_path + '/../data/aws_cluster.json', 'r') as fp:
                cluster_json = json.loads(fp.read())
            if iam_role:
                aws_attr = cluster_json['aws_attributes']
                log.info("Creating cluster with: " + iam_role)
                aws_attr['instance_profile_arn'] = iam_role
                cluster_json['aws_attributes'] = aws_attr
        else:
            with open(real_path + '/../data/azure_cluster.json', 'r') as fp:
                cluster_json = json.loads(fp.read())
        # set the latest spark release regardless of defined cluster json
        # cluster_json['spark_version'] = version['key']
        cluster_name = cluster_json['cluster_name']
        existing_cid = self.get_cluster_id_by_name(cluster_name)
        if existing_cid:
            return existing_cid
        else:
            c_info = self.clusters_service.create_cluster(**cluster_json)
            if c_info['http_status_code'] != 200:
                raise Exception("Could not launch cluster. Verify that the --azure flag or cluster config is correct.")
            self.wait_for_cluster(c_info['cluster_id'])
            return c_info['cluster_id']

    def edit_cluster(self, cid, iam_role):
        """Edits the existing metastore cluster
        Returns cluster_id"""
        version = self.get_latest_spark_version()
        import os
        real_path = os.path.dirname(os.path.realpath(__file__))
        if self.is_aws():
            with open(real_path + '/../data/aws_cluster.json', 'r') as fp:
                cluster_json = json.loads(fp.read())
                # pull AWS attributes and update the IAM policy
                aws_attr = cluster_json['aws_attributes']
                log.info("Updating cluster with: " + iam_role)
                aws_attr['instance_profile_arn'] = iam_role
                cluster_json['aws_attributes'] = aws_attr
                resp = self.clusters_service.edit_cluster(**cluster_json)
                self.wait_for_cluster(cid)
                return cid
        else:
            return False

    def get_execution_context(self, cid):
        log.info("Creating remote Spark Session")
        time.sleep(5)
        ec_payload = {"language": "python",
                      "clusterId": cid}
        ec = self.api_client_v1_2.perform_query("POST", '/contexts/create', data=ec_payload)
        # Grab the execution context ID
        ec_id = ec.get('id', None)
        if not ec_id:
            log.info('Unable to establish remote session')
            log.info(ec)
            raise Exception("Remote session error")
        return ec_id

    def submit_command(self, cid, ec_id, cmd):
        # This launches spark commands and print the results. We can pull out the text results from the API
        command_payload = {'language': 'python',
                           'contextId': ec_id,
                           'clusterId': cid,
                           'command': cmd}
        command = self.api_client_v1_2.perform_query("POST", '/commands/execute',
                                                     data=command_payload, )

        com_id = command.get('id', None)
        if not com_id:
            log.error(f"ERROR: {command}")
        # print('command_id : ' + com_id)
        result_payload = {'clusterId': cid, 'contextId': ec_id, 'commandId': com_id}

        resp = self.api_client_v1_2.perform_query("GET", '/commands/status', data=result_payload)
        is_running = resp['status']

        # loop through the status api to check for the 'running' state call and sleep 1 second
        while (is_running == "Running") or (is_running == 'Queued'):
            resp = self.api_client_v1_2.perform_query("GET", '/commands/status', data=result_payload)
            is_running = resp['status']
            time.sleep(1)
        end_results = resp['results']
        if end_results.get('resultType', None) == 'error':
            log.error(f"ERROR: {end_results.get('summary', None)}")
        return end_results
