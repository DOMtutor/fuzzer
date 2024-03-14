import dataclasses
import enum
import io
import logging
import math
import random
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import *

import yaml

from problemtools import languages, verifyproblem
from problemtools.run import SourceCode, Program
from problemtools.verifyproblem import TestCase, TestCaseGroup, re_argument, SubmissionResult
from pyjudge.repository.kattis import RepositoryProblem, ExecutionError

logger = logging.getLogger(__name__)


class ProblemLayout(object):
    @staticmethod
    def first_failing_case(judge_message, solution):
        tokens = judge_message[0].split()
        if tokens[0] == "TC" or tokens[0] == "Testcase":
            return int(tokens[1][:-1])
        elif tokens[0] == "Wrong":
            test_line = int(tokens[10])
            with solution.open(mode="rt") as f:
                case = 0
                for i, line in enumerate(f.readlines()):
                    if line.startswith('Case #'):
                        case += 1
                    if i + 1 == test_line:
                        return case
        raise ValueError(f"Found no failing test case from message {judge_message}")

    @staticmethod
    def read_to_empty(f):
        res = []
        while line := f.readline().strip():
            res.append(line)
        return res

    def __init__(self, layout_file: Path):
        with layout_file.open(mode="rt") as f:
            empty = sum(not line.strip() for line in f)
            f.seek(0)
            cases = int(f.readline())
            if cases < 3:
                raise ValueError("Given test case does not have at least 3 cases")
            if empty not in [0, 1, cases - 1, cases]:
                raise ValueError("Could not deduce testcase layout")
            self.preamble = False
            self.single_line = False
            if empty == 1 or empty == cases:
                self.preamble = True
            if empty < 2:
                self.single_line = True

    def _read_case(self, f):
        if self.single_line:
            return [f.readline()[:-1]]
        return ProblemLayout.read_to_empty(f)

    def split_case(self, input_file: Path):
        with input_file.open(mode="rt") as f:
            cases = int(f.readline())
            half = cases // 2

            first_part = [str(half)]
            second_part = [str(cases - half)]

            if self.preamble:
                # read and save preamble
                preamble = ProblemLayout.read_to_empty(f)
                first_part += preamble + [""]
                second_part += preamble + [""]

            for i in range(cases):
                destination = first_part if i < half else second_part
                destination += self._read_case(f)
                if not self.single_line:
                    destination += [""]

        if not self.single_line:
            if len(first_part) > 1:
                first_part.pop()
            if len(second_part) > 1:
                second_part.pop()
        return first_part, second_part

    def pick_case(self, input_file, case_number):
        with input_file.open(mode="rt") as f:
            cases = int(f.readline())
            if case_number < 1 or case_number > cases:
                raise ValueError(f"invalid case number {case_number}")

            ret = ["1"]
            if self.preamble:
                ret += ProblemLayout.read_to_empty(f) + [""]
            for _ in range(case_number - 1):
                self._read_case(f)
            ret = ret + self._read_case(f)
            return ret


class RunVerdict(enum.Enum):
    CORRECT = "AC"
    WRONG_ANSWER = "WA"
    RUNTIME_EXCEPTION = "RTE"
    TIME_LIMIT_EXCEEDED = "TLE"
    COMPILE_ERROR = "CE"
    JUDGE_ERROR = "JE"
    FEEDBACK_INCONSISTENCY = "INC"

    @staticmethod
    def get(key):
        for verdict in RunVerdict:
            if verdict.value == key:
                return verdict
        raise KeyError(key)

    def __str__(self):
        return self.name


class RunResult(object):
    def __init__(self, problem, seed_file: Path, input_file: Path, answer_file: Path,
                 verdict: RunVerdict, feedback: Dict[str, str]):
        self.verdict = verdict
        self.feedback = feedback

        self.problem = problem

        with seed_file.open(mode="rt") as f:
            self.seed = f.read()

        with input_file.open(mode="rt") as f:
            self.input = f.read()

        with answer_file.open(mode="rt") as f:
            self.answer = f.read()


class SeedStructure(enum.Enum):
    MULTIPLE_CASES = "multiple"
    SINGLE_CASE = "single"

class FuzzingRun(object):
    RANDOM_RUNS = 200

    @staticmethod
    def parse_feedback(result: SubmissionResult):
        if result.additional_info is None or not result.additional_info.strip():
            return {}
        feedback_files = defaultdict(list)
        current_file = None
        for line in result.additional_info.split("\n"):
            if match := re.search(r"^=== (.*): ===$", line):
                current_file = match.group(1)
            else:
                if current_file is None:
                    raise ValueError(f"Got line {line} without file")
                feedback_files[current_file].append(line.strip())
        return feedback_files

    @staticmethod
    def _strip_comments(line: str) -> str:
        comment_index = line.find('#')
        if comment_index >= 0:
            line = line[:comment_index]
        return line.strip()

    @staticmethod
    def _non_empty_lines(it: Iterable[str]) -> Iterable[str]:
        for line in it:
            line = FuzzingRun._strip_comments(line)
            if line:
                yield line

    @staticmethod
    def detect_seed_type(original: Path) -> Optional[SeedStructure]:
        with original.open(mode="rt") as f:
            candidate = None
            for line in FuzzingRun._non_empty_lines(f.readlines()):
                try:
                    value = int(line)
                except ValueError:
                    return SeedStructure.SINGLE_CASE if candidate is not None else None
                if candidate is None:
                    candidate = value
                elif 0 < candidate <= 20:
                    return SeedStructure.MULTIPLE_CASES


    @staticmethod
    def get_seed(original: Path, structure: SeedStructure):
        with original.open(mode="rt") as f:
            if structure == SeedStructure.SINGLE_CASE:
                for line in FuzzingRun._non_empty_lines(f.readlines()):
                    if line:
                        return int(line)
            elif structure == SeedStructure.MULTIPLE_CASES:
                lines = iter(FuzzingRun._non_empty_lines(f.readlines()))
                next(lines)
                return int(next(lines))
        return None

    @staticmethod
    def randomize_single(original: Path, randomized: Path, seed: str):
        with original.open(mode="rt") as f_o:
            with randomized.open(mode="wt") as f_r:
                lines = iter(FuzzingRun._non_empty_lines(f_o.readlines()))
                next(lines)
                f_r.write(f"{seed}\n")
                for line in lines:
                    f_r.write(f"{line}\n")

    @staticmethod
    def randomize_multiple(original: Path, randomized: Path, cases: int, seed: str):
        with original.open(mode="rt") as f_o:
            with randomized.open(mode="wt") as f_r:
                lines = iter(FuzzingRun._non_empty_lines(f_o.readlines()))
                next(lines)
                next(lines)
                f_r.write(f"{cases}\n")
                f_r.write(f"{seed}\n")
                for line in lines:
                    f_r.write(f"{line}\n")


    def __init__(self, problem: RepositoryProblem, program: Program,
                 submission_logger: logging.Logger, case_seed_file: Path, fuzzing_directory: Path):
        self.problem: RepositoryProblem = problem
        self.program = program
        self.submission_logger = submission_logger

        self.time_limit = problem.limits.time_factor  # TODO Fix with base time

        self.case_seed_file = case_seed_file
        self.seed_type = FuzzingRun.detect_seed_type(self.case_seed_file)
        if self.seed_type is None:
            raise ValueError(f"Incompatible seed file structure {self.case_seed_file}")
        original_seed = FuzzingRun.get_seed(self.case_seed_file, self.seed_type)
        seed_bits = math.floor(math.log(abs(original_seed), 2))
        self.seed = str(abs(random.getrandbits(seed_bits)))

        file_directory = fuzzing_directory / "data"
        file_directory.mkdir(exist_ok=True)
        self.seed_file: Path = file_directory / f"{self.seed}.seed"
        self.input_file: Path = file_directory / f"{self.seed}.in"
        self.answer_file: Path = file_directory / f"{self.seed}.ans"

        self.args = verifyproblem.default_args()
        self.args.bail_on_error = False
        self.args.parts = ["submissions"]
        self.args.problemdir = str(self.problem.directory.absolute())
        self.args.data_filter = re_argument(f"{self.seed}$")
        self.args.use_result_cache = False

        self.test_data = TestCaseGroup(problem.kattis_problem, fuzzing_directory)

    def _write_case(self, case: List[str]):
        with self.input_file.open(mode="wt", encoding="utf-8") as f:
            for line in case:
                f.write(line)
                f.write("\n")

    def _run_submission(self) -> Tuple[SubmissionResult, SubmissionResult, SubmissionResult]:
        time_limit_high = self.time_limit * 2
        self.problem.generate_answer_if_required(self.input_file, self.answer_file)
        return TestCase(self.problem.kattis_problem, str(self.input_file.with_suffix("")), self.test_data) \
            .run_submission(self.program, self.args, self.time_limit, int(self.time_limit + 1), int(time_limit_high))

    def __enter__(self):
        return self

    def evaluate(self) -> RunResult:
        if self.seed_type == SeedStructure.MULTIPLE_CASES:
            FuzzingRun.randomize_multiple(self.case_seed_file, self.seed_file, FuzzingRun.RANDOM_RUNS, self.seed)
            self.problem.generate_input_if_required(self.seed_file, self.input_file)

            result, _, _ = self._run_submission()
            logger.debug("Received initial feedback %s", result)

            if result.verdict is None or result.runtime == -1.0:
                raise ValueError("No executions")

            if result.verdict == "WA":
                logger.debug("Picking failing case")
                self.submission_logger.debug("Found problematic input, picking failing case")
                feedback_files = FuzzingRun.parse_feedback(result)
                failing_case = ProblemLayout.first_failing_case(feedback_files["judgemessage.txt"], self.answer_file)

                layout = ProblemLayout(self.input_file)
                picked_case = layout.pick_case(self.input_file, failing_case)
                self._write_case(picked_case)

                self.submission_logger.debug("Running program again on singular case")
                self.problem.generate_answer_if_required(self.input_file, self.answer_file)
                result, _, _ = self._run_submission()

                run_feedback = FuzzingRun.parse_feedback(result)
                if result.verdict == "WA":
                    run_verdict = RunVerdict.WRONG_ANSWER
                else:
                    run_verdict = RunVerdict.FEEDBACK_INCONSISTENCY
            elif result.verdict == "RTE":
                # binary search for the error
                logger.debug("Search for RTE case")
                self.submission_logger.debug("Runtime error occurred, binary search for the test case")

                layout = ProblemLayout(self.input_file)
                first_half, second_half = layout.split_case(self.input_file)

                while first_half[0] != "0":
                    self._write_case(first_half)

                    self.submission_logger.debug("Running program again on half of remainder")
                    self.problem.generate_answer_if_required(self.input_file, self.answer_file)
                    result, _, _ = self._run_submission()
                    if result.verdict == "RTE":
                        self.submission_logger.debug("RTE occurred in first half")
                    else:
                        self.submission_logger.debug("RTE occurred in second half")
                        self._write_case(second_half)

                    first_half, second_half = layout.split_case(self.input_file)

                self.submission_logger.debug("Should have RTE case now")
                self._write_case(second_half)

                self.submission_logger.debug("Running program on RTE case")
                self.problem.generate_answer_if_required(self.input_file, self.answer_file)
                result, _, _ = self._run_submission()

                run_feedback = FuzzingRun.parse_feedback(result)
                if result.verdict == "RTE":
                    run_verdict = RunVerdict.RUNTIME_EXCEPTION
                else:
                    run_verdict = RunVerdict.FEEDBACK_INCONSISTENCY
            else:
                run_feedback = FuzzingRun.parse_feedback(result)
                run_verdict = RunVerdict.get(result.verdict)

        elif self.seed_type == SeedStructure.SINGLE_CASE:
            FuzzingRun.randomize_single(self.case_seed_file, self.seed_file, self.seed)
            self.problem.generate_input_if_required(self.seed_file, self.input_file)

            result, _, _ = self._run_submission()
            logger.debug("Received feedback %s", result)

            if result.verdict is None or result.runtime == -1.0:
                raise ValueError("No executions")

            run_feedback = FuzzingRun.parse_feedback(result)
            run_verdict = RunVerdict.get(result.verdict)
        else:
            raise AssertionError

        logger.debug("Finished run on %s (with seed %s) with verdict %s",
                     self.case_seed_file.name, self.seed, run_verdict)
        return RunResult(self.problem, self.seed_file, self.input_file, self.answer_file, run_verdict, run_feedback)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for file in [self.input_file, self.seed_file, self.answer_file]:
            try:
                file.unlink(missing_ok=True)
            except IOError as e:
                self.submission_logger.info("Failed to remove file %s", exc_info=e)


@dataclasses.dataclass
class FuzzingRequest(object):
    sources: Dict[str, str]
    language: Optional[str]

    problem: RepositoryProblem
    seed_file: Path

    logger: logging.Logger

    run_count: int = 10


@dataclasses.dataclass
class FuzzingResult(object):
    run_results: Collection[RunResult]


class Fuzzer(object):
    MAX_FAILS = 3

    def __init__(self):
        self.language_config = languages.load_language_config()

    def run(self, request: FuzzingRequest) -> Optional[FuzzingResult]:
        fuzzing_directory = None
        try:
            with tempfile.TemporaryDirectory(prefix="fuzzing-") as tempdir:
                directory = Path(tempdir)
                logger.info("Starting fuzzing")

                output_directory = directory / "fail"
                source_directory = directory / "source"
                compile_directory = directory / "compile"

                fuzzing_directory = directory / "fuzzing"
                for d in [output_directory, source_directory, compile_directory, fuzzing_directory]:
                    d.mkdir(parents=True, exist_ok=True)

                for name, source_code in request.sources.items():
                    source_file = source_directory / name
                    with source_file.open(mode="wt") as source:
                        source.write(source_code)

                    language = self.language_config.languages.get(request.language, None)
                    if language is None:
                        source_files = [str(path) for path in source_directory.iterdir()]
                        language = self.language_config.detect_language(source_files)

                    request.logger.info("Using language %s", language.name)
                    program = SourceCode(str(source_directory), language=language, work_dir=str(compile_directory))

                    request.logger.info("Compiling program")
                    (compilation_result, error) = program.compile()
                    if not compilation_result:
                        raise ValueError(f"Compile error for program {program.name}: {error}")

                    run_results = []

                    request.logger.info("Setting up problem")

                    with request.problem as _:
                        request.logger.info("Starting randomization")
                        fails = 0
                        for i in range(request.run_count):
                            with FuzzingRun(request.problem, program, request.logger,
                                            request.seed_file, fuzzing_directory) as run:
                                run_result = run.evaluate()
                                if run_result.verdict == RunVerdict.FEEDBACK_INCONSISTENCY:
                                    request.logger.warning("Program has feedback inconsistencies")
                                    break
                                if run_result.verdict != RunVerdict.CORRECT:
                                    fails += 1
                                    run_results.append(run_result)

                            request.logger.info("Finished %d runs of %d (%d failed)", i + 1, request.run_count, fails)
                            if fails >= Fuzzer.MAX_FAILS:
                                request.logger.info("Enough runs failed, ending run")
                                break

                        request.logger.info("Fuzzing finished")
                        logger.info("Finished fuzzing")

                        return FuzzingResult(run_results)
        except ExecutionError as e:
            logger.warning("Execution failed with error:\n%s", e.err)
            request.logger.error("Execution failed")
        except ValueError as e:
            logger.warning("Error during fuzzing: %s", exc_info=e)
            request.logger.error("%s", e)
        except Exception as e:
            logger.warning("Error during fuzzing", exc_info=e)
            request.logger.error("Generic error during fuzzing: %s", e)
        finally:
            if fuzzing_directory is not None:
                shutil.rmtree(fuzzing_directory, ignore_errors=True)
        return None
