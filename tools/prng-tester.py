from collections import defaultdict
import itertools
import json
import logging
from multiprocessing import Manager
import os
import random
import subprocess
import sys
import time

import jinja2

from joblib import Parallel, cpu_count, delayed
from randomgen import (
    DSFMT,
    EFIIX64,
    HC128,
    JSF,
    LXM,
    MT19937,
    PCG64,
    SFC64,
    SFMT,
    SPECK128,
    AESCounter,
    ChaCha,
    LCG128Mix,
    Philox,
    Romu,
    ThreeFry,
    Xoshiro256,
    Xoshiro512,
)

ALL_BIT_GENS = [
    AESCounter,
    ChaCha,
    DSFMT,
    EFIIX64,
    HC128,
    JSF,
    LXM,
    PCG64,
    LCG128Mix,
    MT19937,
    Philox,
    SFC64,
    SFMT,
    SPECK128,
    ThreeFry,
    Xoshiro256,
    Xoshiro512,
    Romu,
]
JUMPABLE = [bg for bg in ALL_BIT_GENS if hasattr(bg, "jumped")]

SPECIALS = {
    ChaCha: {"rounds": [8, 20]},
    JSF: {"seed_size": [1, 3]},
    SFC64: {"k": [1, 3394385948627484371, "weyl"]},
    LCG128Mix: {"output": ["upper"]},
    PCG64: {"variant": ["dxsm", "dxsm-128", "xsl-rr"]},
    Romu: {"variant": ["quad", "trio"]},
}
OUTPUT = defaultdict(lambda: 64)
OUTPUT.update({MT19937: 32, DSFMT: 32})
with open("configuration.jinja") as tmpl:
    TEMPLATE = jinja2.Template(tmpl.read())

DSFMT_WRAPPER = """\

class Wrapper32:
    def __init__(self, dsfmt):
        self._bit_gen = dsfmt

    def random_raw(self, n=None):
        return self._bit_gen.random_raw(n).astype("u4")

    def jumped(self):
        return Wrapper32(self._bit_gen.jumped())
"""
# Specials
# SFC64
DEFAULT_ENTOPY = (
    86316980830225721106033794313786972513572058861498566720023788662568817403978
)


def configure_stream(
    bit_gen, kwargs=None, jumped=False, streams=8196, entropy=DEFAULT_ENTOPY
):
    bit_generator = bit_gen.__name__
    extra_code = extra_initialization = ""
    if bit_gen == SFC64 and kwargs["k"] == "weyl":
        extra_code = f"""\
base = rg.SFC64(seed_seq)
weyl = base.weyl_increments({streams})
bitgens = [rg.SFC64(seed_seq, k=k) for k in retain]
        """
    elif bit_gen == DSFMT:
        bit_generator = "Wrapper32"
        extra_initialization = DSFMT_WRAPPER
        # return configure_dsfmt(streams, entropy=entropy)
    kwargs = {} if kwargs is None else kwargs
    kwargs_repr = str(kwargs)
    return TEMPLATE.render(
        bit_generator=bit_generator,
        entropy=entropy,
        jumped=jumped,
        streams=streams,
        kwargs=kwargs_repr,
        output=OUTPUT[bit_gen],
        extra_initialization=extra_initialization,
        extra_code=extra_code,
    )


def get_logger(name=None):
    if name is None:
        logger = logging.getLogger(__name__)
    else:
        logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(name)s - %(asctime)s - %(levelname)s: %(message)s "
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def setup_configuration_files(entropy=DEFAULT_ENTOPY):
    streams = {}
    for bitgen in ALL_BIT_GENS:
        name = bitgen.__name__
        if bitgen not in SPECIALS:
            streams[name] = configure_stream(bitgen, entropy=entropy)
            streams[name + "-streams-4"] = configure_stream(
                bitgen, streams=4, entropy=entropy
            )
            streams[name + "-streams-8196"] = configure_stream(
                bitgen, streams=8196, entropy=entropy
            )
            if bitgen not in JUMPABLE:
                continue
            key = name + "-jumped-streams-4"
            streams[key] = configure_stream(
                bitgen, streams=4, jumped=True, entropy=entropy
            )
            key = name + "-jumped-streams-8196"
            streams[key] = configure_stream(
                bitgen, streams=8196, jumped=True, entropy=entropy
            )
        else:
            config = SPECIALS[bitgen]
            args = [value for value in config.values()]
            for arg_set in itertools.product(*args):
                kwargs = {key: arg for key, arg in zip(config.keys(), arg_set)}
                key = "-".join(
                    [name] + [f"{key}-{value}" for key, value in kwargs.items()]
                )
                streams[key] = configure_stream(bitgen, kwargs=kwargs, entropy=entropy)
                full_key = key + "-streams-4"
                streams[full_key] = configure_stream(
                    bitgen, kwargs=kwargs, streams=4, entropy=entropy
                )
                full_key = key + "-streams-8196"
                streams[full_key] = configure_stream(
                    bitgen, kwargs=kwargs, streams=8196, entropy=entropy
                )
                if bitgen not in JUMPABLE:
                    continue
                full_key = key + "-jumped-streams-4"
                streams[full_key] = configure_stream(
                    bitgen, kwargs=kwargs, streams=4, jumped=True, entropy=entropy
                )
                full_key = key + "-jumped-streams-8196"
                streams[full_key] = configure_stream(
                    bitgen, kwargs=kwargs, streams=8196, jumped=True, entropy=entropy
                )
    return {k: streams[k] for k in sorted(streams.keys())}


def test_single(
    key,
    configurations,
    size="1GB",
    multithreaded=True,
    folding=2,
    expanded=True,
    run_tests=False,
    lock=None,
):
    file_name = os.path.join(os.path.dirname(__file__), f"{key.lower()}.py")
    file_name = os.path.abspath(file_name)
    logger = get_logger(key)
    with open(file_name, "w") as of:
        of.write(configurations[key])

    input_format = "stdin32" if "output = 32" in configurations[key] else "stdin64"
    if not run_tests:
        return

    cmd = [
        "python",
        "practrand-driver.py",
        "-if",
        file_name,
        "|",
        "RNG_test",
        input_format,
        "-tlmax",
        size,
        "-te",
        "1" if expanded else "0",
        "-tf",
        str(folding),
    ]
    if multithreaded:
        cmd += ["-multithreaded"]
    logger.info("Executing " + " ".join(cmd))

    ps = subprocess.Popen(
        " ".join(cmd), shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    output = ps.communicate()[0]
    try:
        os.unlink(file_name)
    except Exception:
        logger.warning(f"Unable to unlink {file_name}")
    if lock is not None:
        with lock:
            with open("results.json", "r") as results_file:
                results = json.load(results_file)
            if key not in results:
                results[key] = {}
            results[key][size] = output.decode("utf8")
            with open("results.json", "w") as results_file:
                json.dump(results, results_file, indent=4, sort_keys=True)
    if "FAIL" in output.decode("utf8"):
        logger.warning("FAIL " + " ".join(cmd))
    else:
        logger.info("Completed " + " ".join(cmd))
    return key, size, output.decode("utf8")


if __name__ == "__main__":
    logger = get_logger("prng-tester")

    import argparse

    parser = argparse.ArgumentParser(
        description="Test alternative configuration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-mt",
        "--multithreaded",
        action="store_true",
        help="Pass the --multithreaded flag in PractRand's RNG_Test.",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        action="store_true",
        help="Use multuprocessing to run the test in parallel.",
    )
    parser.add_argument(
        "-rt",
        "--run-tests",
        action="store_true",
        help="Run the tests. If False, only the test configuration files are output.",
    )
    parser.add_argument(
        "-r",
        "--randomize",
        action="store_true",
        help="Execute in a random order by shuffling.",
    )
    parser.add_argument(
        "-s",
        "--size",
        type=str,
        default="1GB",
        help="Set the size of data ot test using PractRand",
    )
    parser.add_argument(
        "-e",
        "--entropy",
        type=int,
        default=DEFAULT_ENTOPY,
        help="Set the global entropy used in the base SeedSequence used in all of "
        "the test runs",
    )
    parser.add_argument(
        "-n",
        "--n-jobs",
        type=int,
        help="The number of jobs to simultaneously run when using --parallel",
    )
    parser.add_argument(
        "-mj",
        "--max-jobs",
        type=int,
        help="The maximum number of jobs to execute before exiting",
    )
    parser.add_argument(
        "-f",
        "--folding",
        type=int,
        default=1,
        help="The number of folds to use: 0, 1 or 2.",
    )
    parser.add_argument(
        "-ex", "--expanded", action="store_true", help="Use the expanded test suite",
    )
    parser.add_argument(
        "--force", action="store_true", help="Force re-run even if result exists",
    )

    args = parser.parse_args()

    assert args.folding in (0, 1, 2)

    configurations = setup_configuration_files(entropy=args.entropy)
    if args.run_tests:
        print("Running tests...")

        time.sleep(0.5)

    results = defaultdict(dict)

    if os.path.exists("results.json"):
        with open("results.json", "r", encoding="utf8") as existing:
            results.update(json.load(existing))
    manager = Manager()
    lock = manager.Lock()
    configuration_keys = list(configurations.keys())
    if args.randomize:
        random.shuffle(configuration_keys)
        logger.info("Randomizing the execution order")
    final_configuration_keys = []
    for key in configuration_keys:
        if key in results and args.size in results[key] and not args.force:
            logger.info(f"Skipping {key} with size {args.size}")
            continue
        final_configuration_keys.append(key)
    configuration_keys = final_configuration_keys
    if args.max_jobs:
        configuration_keys = configuration_keys[: args.max_jobs]
    if args.parallel:
        test_args = []
        for key in configuration_keys:
            test_args.append(
                [
                    key,
                    configurations,
                    args.size,
                    args.multithreaded,
                    args.folding,
                    args.expanded,
                    args.run_tests,
                    lock,
                ]
            )

        n_jobs = args.n_jobs if args.n_jobs else cpu_count() // 2 - 1
        n_jobs = max(n_jobs, 1)
        logger.info(f"Running in parallel with {n_jobs}.")
        logger.info(f"{len(test_args)} configurations to test")
        parallel_results = Parallel(n_jobs, verbose=50)(
            delayed(test_single)(*ta) for ta in test_args
        )
        for result in parallel_results:
            key = result[0]
            size = result[1]
            value = result[2]
            results[key][size] = value
    else:
        logger.info("Running in series")
        for key in configuration_keys:
            result = test_single(
                key,
                configurations,
                size=args.size,
                multithreaded=args.multithreaded,
                run_tests=args.run_tests,
                lock=lock,
            )
            if args.run_tests:
                key = result[0]
                size = result[1]
                value = result[2]
                results[key][size] = value
                with open("results.json", "w", encoding="utf8") as rf:
                    json.dump(results, rf, indent=4, sort_keys=True)
    with open("results.json", "w", encoding="utf8") as rf:
        json.dump(results, rf, indent=4, sort_keys=True)
