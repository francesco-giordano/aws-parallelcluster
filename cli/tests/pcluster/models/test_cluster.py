# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import json
from unittest.mock import PropertyMock

import pytest
import yaml
from assertpy import assert_that

from pcluster.aws.common import AWSClientError
from pcluster.config.cluster_config import Tag
from pcluster.constants import PCLUSTER_APPLICATION_TAG
from pcluster.models.cluster import Cluster, ClusterActionError, NodeType
from pcluster.models.cluster_resources import ClusterStack
from pcluster.models.s3_bucket import S3Bucket
from tests.pcluster.aws.dummy_aws_api import mock_aws_api
from tests.pcluster.config.dummy_cluster_config import dummy_slurm_cluster_config
from tests.pcluster.models.dummy_s3_bucket import mock_bucket, mock_bucket_object_utils, mock_bucket_utils
from tests.pcluster.test_utils import FAKE_CLUSTER_NAME, FAKE_STACK_NAME

LOG_GROUP_TYPE = "AWS::Logs::LogGroup"
ARTIFACT_DIRECTORY = "s3_artifacts_dir"


class TestCluster:
    @pytest.fixture()
    def cluster(self, mocker):
        mocker.patch(
            "pcluster.models.cluster.Cluster.bucket",
            new_callable=PropertyMock(
                return_value=S3Bucket(
                    service_name=FAKE_CLUSTER_NAME, stack_name=FAKE_STACK_NAME, artifact_directory=ARTIFACT_DIRECTORY
                )
            ),
        )
        return Cluster(FAKE_CLUSTER_NAME, stack=ClusterStack({"StackName": FAKE_STACK_NAME}))

    @pytest.mark.parametrize(
        "node_type, expected_response, expected_instances",
        [
            (NodeType.HEAD_NODE, [{}], 1),
            (NodeType.COMPUTE, [{}, {}, {}], 3),
            (NodeType.COMPUTE, [{}, {}], 2),
            (NodeType.COMPUTE, [], 0),
        ],
    )
    def test_describe_instances(self, cluster, mocker, node_type, expected_response, expected_instances):
        mock_aws_api(mocker)
        mocker.patch(
            "pcluster.aws.ec2.Ec2Client.describe_instances",
            return_value=expected_response,
            expected_params=[
                {"Name": f"tag:{PCLUSTER_APPLICATION_TAG}", "Values": ["test-cluster"]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                {"Name": "tag:parallelcluster:node-type", "Values": [node_type.value]},
            ],
        )

        instances = cluster._describe_instances(node_type=node_type)
        assert_that(instances).is_length(expected_instances)

    @pytest.mark.parametrize(
        "existing_tags", [({}), ({"test": "testvalue"}), ({"Version": "OldVersionToBeOverridden"})]
    )
    def test_tags(self, cluster, mocker, existing_tags):
        """Verify that the function to get the tags list behaves as expected."""
        mock_aws_api(mocker)
        cluster.config = dummy_slurm_cluster_config(mocker)

        # Populate config with list of existing tags
        existing_tags_list = [Tag(key=tag_name, value=tag_value) for tag_name, tag_value in existing_tags.items()]
        cluster.config.tags = existing_tags_list

        # Expected tags:
        installed_version = "FakeInstalledVersion"
        tags = existing_tags
        tags["parallelcluster:version"] = installed_version
        expected_tags_list = self._sort_tags(
            [Tag(key=tag_name, value=tag_value) for tag_name, tag_value in tags.items()]
        )

        # Test method to add version tag
        get_version_patch = mocker.patch(
            "pcluster.models.cluster.get_installed_version", return_value=installed_version
        )
        cluster._add_version_tag()
        assert_that(get_version_patch.call_count).is_equal_to(1)
        assert_that(len(cluster.config.tags)).is_equal_to(len(expected_tags_list))
        assert_that(
            all(
                [
                    source.value == target.value
                    for source, target in zip(self._sort_tags(cluster.config.tags), expected_tags_list)
                ]
            )
        ).is_true()

        # Test method to retrieve CFN tags
        expected_cfn_tags = self._sort_cfn_tags(
            [{"Key": tag_name, "Value": tag_value} for tag_name, tag_value in tags.items()]
        )
        cfn_tags = self._sort_cfn_tags(cluster._get_cfn_tags())
        assert_that(len(cfn_tags)).is_equal_to(len(expected_cfn_tags))
        assert_that(
            all([source["Value"] == target["Value"] for source, target in zip(cfn_tags, expected_cfn_tags)])
        ).is_true()

    @staticmethod
    def _sort_tags(tags):
        return sorted(tags, key=lambda tag: tag.key)

    @staticmethod
    def _sort_cfn_tags(tags):
        return sorted(tags, key=lambda tag: tag["Key"])

    @pytest.mark.parametrize(
        "stack_statuses",
        [
            [
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_COMPLETE",
                "UPDATE_COMPLETE",
            ],
            [
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "anything other than UPDATE_IN_PROGRESS",
                "anything other than UPDATE_IN_PROGRESS",
            ],
            [
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_IN_PROGRESS",
                "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
                "UPDATE_COMPLETE",
            ],
            ["UPDATE_COMPLETE", "UPDATE_COMPLETE"],
        ],
    )
    def test_wait_for_stack_update(self, cluster, mocker, stack_statuses):
        """
        Verify that _wait_for_stack_update behaves as expected.

        _wait_for_stack_update should call updated_status until the StackStatus is anything besides UPDATE_IN_PROGRESS
        and UPDATE_COMPLETE_CLEANUP_IN_PROGRESS.
        use that to get expected call count for updated_status
        """
        expected_call_count = len(stack_statuses)
        updated_status_mock = mocker.patch.object(cluster, "_get_updated_stack_status", side_effect=stack_statuses)
        mocker.patch("pcluster.models.cluster.time.sleep")  # so we don't actually have to wait

        cluster._wait_for_stack_update()
        assert_that(updated_status_mock.call_count).is_equal_to(expected_call_count)

    @pytest.mark.parametrize(
        "template_body,error_message",
        [
            ({"TemplateKey": "TemplateValue"}, None),
            ({}, "Unable to retrieve template for stack {0}.*".format(FAKE_STACK_NAME)),
            (None, "Unable to retrieve template for stack {0}.*".format(FAKE_STACK_NAME)),
        ],
    )
    def test_get_stack_template(self, cluster, mocker, template_body, error_message):
        """Verify that _get_stack_template method behaves as expected."""
        response = json.dumps(template_body) if template_body is not None else error_message
        mock_aws_api(mocker)
        mocker.patch(
            "pcluster.aws.cfn.CfnClient.get_stack_template",
            return_value=response,
            expected_params=FAKE_STACK_NAME,
            side_effect=AWSClientError(function_name="get_template", message="error") if not template_body else None,
        )

        if error_message:
            with pytest.raises(ClusterActionError, match=error_message):
                _ = cluster._get_stack_template()
        else:
            assert_that(cluster._get_stack_template()).is_equal_to(yaml.load(response))

    @pytest.mark.parametrize(
        "error_message",
        [
            None,
            "No UpDatES ARE TO BE PERformed",
            "some longer message also containing no updates are to be performed and more words at the end"
            "some other error message",
        ],
    )
    def test_update_stack_template(self, cluster, mocker, error_message):
        """Verify that _update_stack_template behaves as expected."""
        template_body = {"TemplateKey": "TemplateValue"}
        template_url = "https://{bucket_name}.s3.{region}.amazonaws.com{partition_suffix}/{template_key}"
        response = error_message or {"StackId": "stack ID"}

        mock_aws_api(mocker)
        mocker.patch("pcluster.aws.cfn.CfnClient.get_stack_template", return_value=template_body)
        mocker.patch(
            "pcluster.aws.cfn.CfnClient.update_stack_from_url",
            return_value=response,
            expected_params={
                "stack_name": FAKE_STACK_NAME,
                "template_url": template_url,
            },
            side_effect=AWSClientError(function_name="update_stack_from_url", message=error_message)
            if error_message is not None
            else None,
        )

        # mock bucket initialize
        mock_bucket(mocker)
        # mock bucket utils
        mock_bucket_utils(mocker)
        # mock bucket object utils
        mock_bucket_object_utils(mocker)

        wait_for_update_mock = mocker.patch.object(cluster, "_wait_for_stack_update")

        if error_message is None or "no updates are to be performed" in error_message.lower():
            cluster._update_stack_template(template_body)
            if error_message is None or "no updates are to be performed" not in error_message.lower():
                assert_that(wait_for_update_mock.called).is_true()
            else:
                assert_that(wait_for_update_mock.called).is_false()
        else:
            full_error_message = "Unable to update stack template for stack {stack_name}: {emsg}".format(
                stack_name=FAKE_STACK_NAME, emsg=error_message
            )
            with pytest.raises(AWSClientError, match=full_error_message) as sysexit:
                cluster._update_stack_template(template_url)
            assert_that(sysexit.value.code).is_not_equal_to(0)

    @pytest.mark.parametrize(
        "keep_logs,persist_called,terminate_instances_called",
        [
            (False, False, True),
            (False, False, True),
            (True, True, True),
        ],
    )
    def test_delete(self, cluster, mocker, keep_logs, persist_called, terminate_instances_called):
        """Verify that delete behaves as expected."""
        mocker.patch.object(cluster.stack, "delete")
        mock_aws_api(mocker)
        mocker.patch("pcluster.aws.cfn.CfnClient.describe_stack")
        mocker.patch("pcluster.aws.cfn.CfnClient.delete_stack")
        persist_cloudwatch_log_groups_mock = mocker.patch.object(cluster, "_persist_cloudwatch_log_groups")
        terminate_nodes_mock = mocker.patch.object(cluster, "_terminate_nodes")

        cluster.delete(keep_logs)

        assert_that(persist_cloudwatch_log_groups_mock.called).is_equal_to(persist_called)
        assert_that(terminate_nodes_mock.call_count).is_equal_to(1 if terminate_instances_called else 0)

    @pytest.mark.parametrize(
        "template, expected_retain, fail_on_persist",
        [
            ({}, False, False),
            (
                {"Resources": {"key": {"DeletionPolicy": "Retain"}}},
                True,
                False,
            ),
            (
                {"Resources": {"key": {"DeletionPolicy": "Retain"}}},
                True,
                True,
            ),
            (
                {"Resources": {"key": {"DeletionPolicy": "Don't Retain"}}},
                False,
                False,
            ),
            (
                {"Resources": {"key": {"DeletionPolicy": "Delete"}}},
                False,
                False,
            ),
        ],
    )
    def test_persist_cloudwatch_log_groups(self, cluster, mocker, caplog, template, expected_retain, fail_on_persist):
        """Verify that _persist_cloudwatch_log_groups behaves as expected."""
        mocker.patch("pcluster.models.cluster.Cluster._get_artifact_dir")
        mocker.patch("pcluster.models.cluster.Cluster._get_stack_template", return_value=template)

        client_error = AWSClientError("function", "Generic error.")
        update_template_mock = mocker.patch.object(
            cluster, "_update_stack_template", side_effect=client_error if fail_on_persist else None
        )
        mock_aws_api(mocker)
        mocker.patch("pcluster.aws.cfn.CfnClient.update_stack_from_url")
        mock_bucket(mocker)
        mock_bucket_utils(mocker)
        mock_bucket_object_utils(mocker)

        if expected_retain:
            keys = ["key"]
        else:
            keys = []
        get_unretained_cw_log_group_resource_keys_mock = mocker.patch.object(
            cluster, "_get_unretained_cw_log_group_resource_keys", return_value=keys
        )

        if fail_on_persist:
            with pytest.raises(ClusterActionError) as e:
                cluster._persist_cloudwatch_log_groups()
            assert_that(str(e)).contains("Unable to persist logs")
        else:
            cluster._persist_cloudwatch_log_groups()

        assert_that(get_unretained_cw_log_group_resource_keys_mock.call_count).is_equal_to(1)
        assert_that(update_template_mock.call_count).is_equal_to(1 if expected_retain else 0)

    @pytest.mark.parametrize(
        "template",
        [
            {},
            {"Resources": {}},
            {"Resources": {"key": {}}},
            {"Resources": {"key": {"DeletionPolicy": "Don't Retain"}}},
            {"Resources": {"key": {"DeletionPolicy": "Delete"}}},
            {"Resources": {"key": {"DeletionPolicy": "Retain"}}},  # Note update_stack_template still called for this
        ],
    )
    def test_persist_stack_resources(self, cluster, mocker, template):
        """Verify that _persist_stack_resources behaves as expected."""
        mocker.patch("pcluster.models.cluster.Cluster._get_artifact_dir")
        mocker.patch("pcluster.models.cluster.Cluster._get_stack_template", return_value=template)
        update_stack_template_mock = mocker.patch("pcluster.models.cluster.Cluster._update_stack_template")
        mock_aws_api(mocker)
        mocker.patch("pcluster.aws.cfn.CfnClient.update_stack_from_url")
        mock_bucket(mocker)
        mock_bucket_utils(mocker)
        mock_bucket_object_utils(mocker)

        if "Resources" not in template:
            expected_error_message = "Resources"
        elif "key" not in template.get("Resources"):
            expected_error_message = "key"
        else:
            expected_error_message = None

        if expected_error_message:
            with pytest.raises(KeyError, match=expected_error_message):
                cluster._persist_stack_resources(["key"])
            assert_that(update_stack_template_mock.called).is_false()
        else:
            cluster._persist_stack_resources(["key"])
            assert_that(update_stack_template_mock.called).is_true()
            assert_that(cluster._get_stack_template()["Resources"]["key"]["DeletionPolicy"]).is_equal_to("Retain")

    @pytest.mark.parametrize(
        "template,expected_return",
        [
            ({}, []),
            ({"Resources": {}}, []),
            ({"Resources": {"ResourceOne": {"Type": LOG_GROUP_TYPE, "DeletionPolicy": "Retain"}}}, []),
            ({"Resources": {"ResourceOne": {"Type": LOG_GROUP_TYPE, "DeletionPolicy": "NotRetain"}}}, ["ResourceOne"]),
            ({"Resources": {"ResourceOne": {"Type": LOG_GROUP_TYPE, "DeletionPolicy": "Delete"}}}, ["ResourceOne"]),
        ],
    )
    def test_get_unretained_cw_log_group_resource_keys(self, cluster, mocker, template, expected_return):
        """Verify that _get_unretained_cw_log_group_resource_keys behaves as expected."""
        mocker.patch("pcluster.models.cluster.Cluster._get_stack_template", return_value=template)
        observed_return = cluster._get_unretained_cw_log_group_resource_keys()
        assert_that(observed_return).is_equal_to(expected_return)
