# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
import csv
import logging
import pathlib
import re
from datetime import datetime
from shutil import copyfile

from constants import OSU_BENCHMARK_VERSION
from utils import render_jinja_template

OSU_COMMON_DATADIR = pathlib.Path(__file__).parent / "data/osu/"
REFERENCE_RESULTS_FILE = OSU_COMMON_DATADIR / "reference_results.csv"
SUPPORTED_MPIS = ["openmpi", "intelmpi"]


def compile_osu(mpi_variant, remote_command_executor):
    init_script = render_jinja_template(
        template_file_path=OSU_COMMON_DATADIR / "init_osu_benchmarks.sh", osu_benchmark_version=OSU_BENCHMARK_VERSION
    )
    remote_command_executor.run_remote_script(
        str(init_script),
        args=[mpi_variant],
        hide=True,
        additional_files=[
            str(OSU_COMMON_DATADIR / f"osu-micro-benchmarks-{OSU_BENCHMARK_VERSION}.tgz"),
            str(OSU_COMMON_DATADIR / "config.guess"),
            str(OSU_COMMON_DATADIR / "config.sub"),
        ],
    )


def run_individual_osu_benchmark(
    mpi_version,
    benchmark_group,
    benchmark_name,
    partition,
    remote_command_executor,
    scheduler_commands,
    num_instances,
    slots_per_instance,
    test_datadir,
    submission_script_template_path=None,
    rendered_template_path=None,
    timeout=None,
):
    """
    Run the given OSU benchmark.

    :param mpi_version: string, should be one of SUPPORTED_MPIS
    :param benchmark_group: string, which of the MPI benchmarks to run. As of 5.7.1 this includes collective, one-sided,
                            pt2pt, and startup
    :param benchmark_name: string, name of the benchmark to run from the given group
    :param partition: string, partition on which to benchmark job (assumes the use of Slurm scheduler)
    :param remote_command_executor: RemoteCommandExecutor instance, used to submit jobs
    :param scheduler_commands: SchedulerlurmCommands instance, used to submit jobs
    :param num_instances: int, number of instances to run benchmark across
    :param slots_per_instance: int, number of processes to run on each node
    :param test_datadir: Path, used to construct default output path when rendering submission script template
    :param submission_script_template_path: string, override default path for source submission script template
    :param rendered_template_path: string, override destination path when rendering submission script template
    :param timeout: int, maximum number of minutes to wait for job to complete
    :return: string, stdout of the benchmark job
    """
    logging.info(f"Running OSU benchmark {OSU_BENCHMARK_VERSION}: {benchmark_name} for {mpi_version}")

    if mpi_version not in SUPPORTED_MPIS:
        raise Exception(f"Unsupported MPI: '{mpi_version}'. Must be one of {' '.join(SUPPORTED_MPIS)}")

    compile_osu(mpi_version, remote_command_executor)

    # Prepare submission script and pass to the scheduler for the job submission
    if not submission_script_template_path:
        submission_script_template_path = OSU_COMMON_DATADIR / f"osu_{benchmark_group}_submit_{mpi_version}.sh"
    if not rendered_template_path:
        rendered_template_path = test_datadir / f"osu_{benchmark_group}_submit_{mpi_version}_{benchmark_name}.sh"
    copyfile(submission_script_template_path, rendered_template_path)
    slots = num_instances * slots_per_instance
    submission_script = render_jinja_template(
        template_file_path=rendered_template_path,
        benchmark_name=benchmark_name,
        osu_benchmark_version=OSU_BENCHMARK_VERSION,
        num_of_processes=slots,
    )
    if partition:
        result = scheduler_commands.submit_script(
            str(submission_script), slots=slots, partition=partition, nodes=num_instances
        )
    else:
        result = scheduler_commands.submit_script(str(submission_script), slots=slots, nodes=num_instances)
    job_id = scheduler_commands.assert_job_submitted(result.stdout)
    scheduler_commands.wait_job_completed(job_id, timeout=timeout)
    scheduler_commands.assert_job_succeeded(job_id)

    output = remote_command_executor.run_remote_command(f"cat /shared/{benchmark_name}.out").stdout
    return job_id, output


def _csv_str_to_float(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def results_csv_to_dict() -> dict:
    filename = REFERENCE_RESULTS_FILE
    dict_result = {}
    with open(filename, newline="", encoding="utf-8-sig") as results:
        reader = csv.DictReader(results, delimiter=";")
        for row in reader:
            mpi_version = row["mpi_version"]
            test_name = row["test_name"]
            packet_size = row["packet_size"]

            dict_result.setdefault(mpi_version, {})
            dict_result[mpi_version].setdefault(test_name, {})
            dict_result[mpi_version][test_name].setdefault(packet_size, {})

            dict_result[mpi_version][test_name][packet_size]["average"] = float(_csv_str_to_float(row["average"]))
            dict_result[mpi_version][test_name][packet_size]["std"] = float(_csv_str_to_float(row["std"]))
    return dict_result


def check_thresholds(reference_results: dict, metric_data: dict, variability_factor: int = 5) -> list:
    """_summary_

    Args:
        reference_results (dict): The reference results used to compare the threshold.
        If empty the function return immediately
        metric_data (dict): The results from the benchmarks in the cloudwatch format
        variability_factor (int, optional): The sensibility of the check: greater
        is the value lower will be the sensibility. Defaults to 5.

    Returns:
        list: The benchmarks which are failed
    """
    if reference_results == {}:
        return []

    accepted_number_of_failures = 3
    failing_benchmarks = []
    failing_benchmarks_by_test = {}
    for metric in metric_data:
        # Read from the CloudWatch format the relevant variable to check the thresholds
        if metric["MetricName"] == "Latency":
            for dimension in metric["Dimensions"]:
                name = dimension["Name"]
                value = dimension["Value"]

                if name == "PacketSize":
                    packet_size = value
                elif name == "OsuBenchmarkName":
                    test_name = value
                elif name == "MpiVariant":
                    mpi_variant = value

        # Check if the specific packet_size exist in the reference results because it can be missing due to the
        # default of OSU benchmarks which limits to 512MB the memory allocations
        if reference_results.get(mpi_variant, {}).get(test_name, {}).get(packet_size):
            threshold = reference_results[mpi_variant][test_name][packet_size]["average"] + (
                variability_factor * reference_results[mpi_variant][test_name][packet_size]["std"]
            )
        else:
            logging.warning(
                "Reference result not found for mpi variant "
                f"'{mpi_variant}' test '{test_name}' and packet size '{packet_size}'"
            )
            # If the threshold cannot be defined, go to the next value
            continue

        message = (
            f"{mpi_variant} - {test_name} - packet size {packet_size}: "
            f"tolerated: {threshold}, current: {metric['Value']}"
        )
        if metric["Value"] > threshold:
            failing_benchmarks_by_test.setdefault(test_name, [])
            failing_benchmarks_by_test[test_name].append(message)
            logging.error(message)
        else:
            logging.info(message)

    # Create a list from the failure by test which exceeds the accepted_number_of_failures
    for test_name in failing_benchmarks_by_test:
        if len(failing_benchmarks_by_test[test_name]) > accepted_number_of_failures:
            failing_benchmarks.append(failing_benchmarks_by_test[test_name])

    return failing_benchmarks


def run_osu_benchmarks(
    osu_benchmarks,
    mpi_variant,
    partition,
    remote_command_executor,
    scheduler_commands,
    num_instances,
    slots_per_instance,
    region,
    instance,
    test_datadir,
    dimensions,
):
    for osu_benchmark_group, osu_benchmark_names in osu_benchmarks.items():
        for osu_benchmark_name in osu_benchmark_names:
            dimensions_copy = dimensions.copy()
            logging.info("Running benchmark %s", osu_benchmark_name)
            job_id, output = run_individual_osu_benchmark(
                mpi_version=mpi_variant,
                benchmark_group=osu_benchmark_group,
                benchmark_name=osu_benchmark_name,
                partition=partition,
                remote_command_executor=remote_command_executor,
                scheduler_commands=scheduler_commands,
                num_instances=num_instances,
                slots_per_instance=slots_per_instance,
                test_datadir=test_datadir,
                timeout=40,
            )
            logging.info("Preparing benchmarks %s metrics", osu_benchmark_name)
            metric_data = []
            submit_time = datetime.strptime(scheduler_commands.get_job_submit_time(job_id), "%Y-%m-%dT%H:%M:%S")
            start_time = datetime.strptime(scheduler_commands.get_job_start_time(job_id), "%Y-%m-%dT%H:%M:%S")
            wait_seconds = (start_time - submit_time).total_seconds()
            if wait_seconds >= 15:
                # After submission, if job waited more than 15 seconds before running, the job was probably
                # waiting for compute nodes to be launched. Therefore, the wait time is pushed to CloudWatch
                # as an indicator of how fast the compute nodes were launched.
                metric_data.append(
                    {
                        "MetricName": "JobWaitTime",
                        "Dimensions": [{"Name": name, "Value": str(value)} for name, value in dimensions_copy.items()],
                        "Value": wait_seconds,
                        "Unit": "Seconds",
                    }
                )
            for packet_size, latency in re.findall(r"(\d+)\s+(\d+)\.", output):
                dimensions_copy.update(
                    {
                        "OsuBenchmarkGroup": osu_benchmark_group,
                        "OsuBenchmarkName": osu_benchmark_name,
                        "PacketSize": packet_size,
                    }
                )
                metric_data.append(
                    {
                        "MetricName": "Latency",
                        "Dimensions": [{"Name": name, "Value": str(value)} for name, value in dimensions_copy.items()],
                        "Value": int(latency),
                        "Unit": "Microseconds",
                    }
                )
            yield metric_data
