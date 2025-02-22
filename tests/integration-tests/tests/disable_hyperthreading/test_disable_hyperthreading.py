# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and limitations under the License.
import logging
import re

import pytest
from assertpy import assert_that
from remote_command_executor import RemoteCommandExecutor

from tests.common.assertions import assert_no_errors_in_logs
from tests.common.utils import fetch_instance_slots, run_system_analyzer


@pytest.mark.usefixtures("os")
def test_hit_disable_hyperthreading(
    region,
    scheduler,
    instance,
    pcluster_config_reader,
    clusters_factory,
    default_threads_per_core,
    run_benchmarks,
    benchmarks,
    scheduler_commands_factory,
    request,
):
    """Test Disable Hyperthreading for HIT clusters."""
    slots_per_instance = fetch_instance_slots(region, instance)
    cluster_config = pcluster_config_reader(benchmarks=benchmarks)
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = scheduler_commands_factory(remote_command_executor)
    _test_disable_hyperthreading_settings(
        remote_command_executor,
        scheduler_commands,
        slots_per_instance,
        scheduler,
        hyperthreading_disabled=False,
        partition="ht-enabled",
        default_threads_per_core=default_threads_per_core,
    )
    _test_disable_hyperthreading_settings(
        remote_command_executor,
        scheduler_commands,
        slots_per_instance,
        scheduler,
        hyperthreading_disabled=True,
        partition="ht-disabled",
        default_threads_per_core=default_threads_per_core,
    )

    assert_no_errors_in_logs(remote_command_executor, scheduler)
    run_system_analyzer(cluster, scheduler_commands_factory, request)
    run_benchmarks(remote_command_executor, scheduler_commands)


def _test_disable_hyperthreading_settings(
    remote_command_executor,
    scheduler_commands,
    slots_per_instance,
    scheduler,
    hyperthreading_disabled=True,
    partition=None,
    default_threads_per_core=2,
):
    expected_cpus_per_instance = (
        slots_per_instance // default_threads_per_core if hyperthreading_disabled else slots_per_instance
    )
    expected_threads_per_core = 1 if hyperthreading_disabled else default_threads_per_core

    # Test disable hyperthreading on head node
    logging.info("Test Disable Hyperthreading on head node")
    result = remote_command_executor.run_remote_command("lscpu")
    if partition:
        # If partition is supplied, assume this is HIT setting where ht settings are at the queue level
        # In this case, ht is not disabled on head node
        assert_that(result.stdout).matches(r"Thread\(s\) per core:\s+{0}".format(default_threads_per_core))
        _assert_active_cpus(result.stdout, slots_per_instance)
    else:
        assert_that(result.stdout).matches(r"Thread\(s\) per core:\s+{0}".format(expected_threads_per_core))
        _assert_active_cpus(result.stdout, expected_cpus_per_instance)

    # Test disable hyperthreading on Compute
    logging.info("Test Disable Hyperthreading on Compute")
    if partition:
        result = scheduler_commands.submit_command("lscpu > /shared/lscpu.out", partition=partition)
    else:
        result = scheduler_commands.submit_command("lscpu > /shared/lscpu.out")
    job_id = scheduler_commands.assert_job_submitted(result.stdout)
    scheduler_commands.wait_job_completed(job_id)
    scheduler_commands.assert_job_succeeded(job_id)

    # Check compute has 1 thread per core
    result = remote_command_executor.run_remote_command("cat /shared/lscpu.out")
    assert_that(result.stdout).matches(r"Thread\(s\) per core:\s+{0}".format(expected_threads_per_core))
    _assert_active_cpus(result.stdout, expected_cpus_per_instance)

    # Check scheduler has correct number of cores
    if partition:
        result = scheduler_commands.get_node_cores(partition=partition)
    else:
        result = scheduler_commands.get_node_cores()
    logging.info("{0} Cores: [{1}]".format(scheduler, result))
    assert_that(int(result)).is_equal_to(expected_cpus_per_instance)

    if hyperthreading_disabled:
        # check scale up to 2 nodes
        if partition:
            result = scheduler_commands.submit_command(
                "hostname > /shared/hostname.out", slots=2 * expected_cpus_per_instance, partition=partition
            )
        else:
            result = scheduler_commands.submit_command(
                "hostname > /shared/hostname.out", slots=2 * expected_cpus_per_instance
            )
        job_id = scheduler_commands.assert_job_submitted(result.stdout)
        scheduler_commands.wait_job_completed(job_id)
        scheduler_commands.assert_job_succeeded(job_id)
        if partition:
            assert_that(scheduler_commands.compute_nodes_count(filter_by_partition=partition)).is_equal_to(2)
        else:
            assert_that(scheduler_commands.compute_nodes_count()).is_equal_to(2)


def _assert_active_cpus(lscpu_output, expected_cpus):
    # Compute the number of active cpus based on the output of "On-line CPUs list"
    # Examples:
    # 1,2,3 => active cpus = 3 (1, 2, 3)
    # 3-5 => active cpus = 3 (3, 4, 5)
    # 1, 3-6 => active cpus = 5 (1, 3, 4, 5, 6)
    online_cpus_list = re.search(r"On-line CPU\(s\) list:\s*(.*)", lscpu_output).group(1)
    active_cpus = 0
    for cpus_interval in online_cpus_list.split(","):
        cpu_interval_tokens = cpus_interval.split("-")
        if len(cpu_interval_tokens) > 1:
            num_cpus = abs(int(cpu_interval_tokens[1].strip()) - int(cpu_interval_tokens[0].strip())) + 1
        else:
            num_cpus = 1
        active_cpus += num_cpus

    assert_that(active_cpus).is_equal_to(expected_cpus)
