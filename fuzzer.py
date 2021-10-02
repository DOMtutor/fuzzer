import sys
import logging
import os
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from io import StringIO
from pathlib import Path
from typing import *

sys.path.extend(["repository/kattis", "repository/scripts"])

import model.model as model
from repository.repository import RepositoryProblem, ProblemRepository

from problemtools import languages, verifyproblem
from problemtools.run import SourceCode, Program
from problemtools.verifyproblem import ProblemAspect, Problem as KattisProblem, \
    TestCaseGroup, re_argument, SubmissionResult, TestCase

logger = logging.getLogger(__name__)


class MakeError(Exception):
    def __init__(self, rule, out, err):
        self.rule = rule
        self.out = out
        self.err = err


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


class RunResult(object):
    def __init__(self, problem, seed_file: Path, input_file: Path, answer_file: Path,
                 verdict: str, feedback: Dict[str, str]):
        self.verdict = verdict
        self.feedback = feedback

        self.problem = problem
        self.seed_file = seed_file
        self.input_file = input_file
        self.answer_file = answer_file

    def copy_data(self, output_dir: Path):
        output_dir.mkdir(parents=True)

        with (output_dir / "verdict").open("wt") as f:
            f.write(self.verdict)

        shutil.copy(self.seed_file, output_dir / "case.seed")
        shutil.copy(self.input_file, output_dir / "case.in")
        shutil.copy(self.answer_file, output_dir / "case.ans")

        program_output = Path(self.problem.tmpdir) / "output"
        if os.path.isfile(program_output):
            shutil.copy(program_output, output_dir / "case.out")
        for name, content in self.feedback.items():
            with (output_dir / name).open(mode="wt") as f:
                f.writelines(content)



class FuzzingRun(object):
    RANDOM_RUNS = 200

    @staticmethod
    def parse_feedback(result: SubmissionResult):
        if result.additional_info is None:
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
    def randomize(original: Path, randomized: Path, cases: int, seed: str):
        with original.open(mode="rt") as f_o:
            with randomized.open(mode="wt") as f_r:
                written = 0
                for line in f_o.readlines():
                    if not line.startswith('#'):
                        if written == 0:
                            f_r.write(f"{str(cases)}\n")
                        elif written == 1:
                            f_r.write(f"{seed}\n")
                        else:
                            f_r.write(line)
                        written += 1

    def __init__(self, time_limit: float, problem: KattisProblem, program: Program,
                 submission_logger: logging.Logger, case_seed_file, fuzzing_directory):
        self.time_limit = time_limit
        self.problem: KattisProblem = problem
        self.program = program
        self.submission_logger = submission_logger

        self.alternate_limit = False

        self.case_seed_file = case_seed_file
        self.seed = str(random.getrandbits(63))
        self.seed_file: Path = fuzzing_directory / f"fuzzing_{self.seed}.seed"
        self.input_file: Path = fuzzing_directory / f"fuzzing_{self.seed}.in"
        self.answer_file: Path = fuzzing_directory / f"fuzzing_{self.seed}.ans"
        self.problem_directory: Path = pathlib.Path(problem.probdir)

        self.args = verifyproblem.default_args()
        self.args.bail_on_error = False
        self.args.parts = ["submissions"]
        self.args.problemdir = self.problem.probdir
        self.args.data_filter = re_argument(f"fuzzing_{self.seed}$")

        # Need to make the case before creating the test case group
        FuzzingRun.randomize(self.case_seed_file, self.seed_file, FuzzingRun.RANDOM_RUNS, self.seed)
        self.run_make(self.input_file)
        self.run_make(self.answer_file)

        self.test_data = TestCaseGroup(problem, fuzzing_directory)

    def write_case(self, case: List[str]):
        with self.input_file.open(mode="wt", encoding="utf-8") as f:
            for line in case:
                f.write(line)
                f.write("\n")

    def run_submission(self) -> Tuple[SubmissionResult, SubmissionResult]:
        self.run_make(self.answer_file)

        # Trick the cache key ...
        time_limit_high = self.time_limit * 2 + (0.001 if self.alternate_limit else 0.0)
        self.alternate_limit = not self.alternate_limit
        return self.test_data.run_submission(self.program, self.args, self.time_limit, time_limit_high)

    def run_make(self, rule: Union[str, Path]):
        logger.debug("Make %s", rule)
        if isinstance(rule, Path):
            rule = rule.relative_to(self.problem_directory)
        process = subprocess.Popen(["make", rule], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", cwd=str(self.problem_directory))
        out, err = process.communicate(timeout=10)
        if process.returncode != 0:
            raise MakeError(rule, out, err)

    def __enter__(self):
        self.submission_logger.debug("Running program on submission")
        (result1, result2) = self.run_submission()
        self.submission_logger.debug("Received feedback %s / %s", result1.verdict, result2.verdict)

        if result1.verdict == "WA":
            self.submission_logger.debug("Found problematic input, picking failing case")
            feedback_files = FuzzingRun.parse_feedback(result1)
            failing_case = ProblemLayout.first_failing_case(feedback_files["judgemessage.txt"], self.answer_file)

            layout = ProblemLayout(self.input_file)
            picked_case = layout.pick_case(self.input_file, failing_case)
            self.write_case(picked_case)

            self.submission_logger.debug("Running program again on singular case")
            (result1, result2) = self.run_submission()

            run_feedback = FuzzingRun.parse_feedback(result1)
            if result1.verdict == "WA":
                run_verdict = "WA"
            else:
                run_verdict = "INC"
        elif result1.verdict == "RTE":
            # binary search for the error
            self.submission_logger.debug("Runtime error occurred, binary search for the test case")

            layout = ProblemLayout(self.input_file)
            first_half, second_half = layout.split_case(self.input_file)

            while first_half[0] != "0":
                self.write_case(first_half)

                self.submission_logger.debug("Running program again on half of remainder")
                (result1, result2) = self.run_submission()
                if result1.verdict == "RTE":
                    self.submission_logger.debug("RTE occurred in first half")
                else:
                    self.submission_logger.debug("RTE occurred in second half")
                    self.write_case(second_half)

                first_half, second_half = layout.split_case(self.input_file)

            self.submission_logger.debug("Should have RTE case now")
            self.write_case(second_half)

            self.submission_logger.debug("Running program on RTE case")
            (result1, result2) = self.run_submission()

            run_feedback = FuzzingRun.parse_feedback(result1)
            if result1.verdict == "RTE":
                run_verdict = "RTE"
            else:
                run_verdict = "INC"
        else:
            run_feedback = FuzzingRun.parse_feedback(result1)
            run_verdict = result1.verdict

        return RunResult(self.problem, self.seed_file, self.input_file, self.answer_file, run_verdict, run_feedback)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for file in [self.input_file, self.seed_file, self.answer_file]:
            try:
                file.unlink(missing_ok=True)
            except IOError as e:
                self.submission_logger.info("Failed to remove file %s", exc_info=e)


class Fuzzer(object):
    MAX_FAILS = 3

    def __init__(self, case: str, source_directory: pathlib.Path, language: model.Language,
                 problem: RepositoryProblem, output_directory: pathlib.Path, run_count: int,
                 submission_logger: logging.Logger, time_limit):
        self.case = case
        self.problem = problem
        self.source_directory = source_directory.resolve().absolute()
        self.output_directory = output_directory.resolve().absolute()
        self.submission_logger = submission_logger
        self.time_limit = time_limit
        self.language = language
        self.run_count = run_count

        self.problem_data_directory = self.problem.directory / 'data'

    def run_random_case(self):
        ProblemAspect.silent = True

        fuzzing_directory = self.problem_data_directory / 'fuzzing'
        fuzzing_directory.mkdir(exist_ok=True)
        case_seed_file = self.problem_data_directory / 'secret' / (self.case + '.seed')

        if not case_seed_file.is_file():
            raise ValueError(f"Could not locate seed file {case_seed_file}")

        # self.run_make("checker")

        with KattisProblem(self.problem.directory) as problem:
            program = SourceCode(str(self.source_directory.absolute()),
                                 language=self.language, work_dir=problem.tmpdir)

            self.submission_logger.info("Compiling program %s", program.name)
            (compilation_result, error) = program.compile()
            if not compilation_result:
                raise ValueError(f"Compile error for program {program.name}: {error}")

            # self.run_make("generators")

            fails = 0
            for i in range(self.run_count):
                with FuzzingRun(self.time_limit, problem, program, self.submission_logger,
                                case_seed_file, fuzzing_directory) as result:
                    if result.verdict == "AC":
                        continue
                    if result.verdict == "INC":
                        self.submission_logger.warning("Program has feedback inconsistencies")
                        return
                    fails += 1
                    run_output_directory = self.output_directory / f"{fails}"
                    result.copy_data(run_output_directory)

                self.submission_logger.info("Finished %d runs of %d on %s (%d failed)",
                                            i + 1, self.run_count, self.case, fails)

                if fails >= Fuzzer.MAX_FAILS:
                    self.submission_logger.info("Enough runs failed, ending run")
                    return


class FuzzingThread(threading.Thread):
    TIMEOUT = 5
    FORMATTER = logging.Formatter("%(asctime)s - %(message)s")

    @staticmethod
    def read_results(output_directory):
        result = {}
        for case_dir in output_directory.iterdir():
            if case_dir.is_dir():
                category = None
                data = {}
                for file in case_dir.iterdir():
                    if file.name == "verdict":
                        category = file.read_text("utf-8")
                    else:
                        data[file.name] = file.read_text("utf-8")
                if category is not None:
                    result[f"{category}_{case_dir.name}"] = data
        return result

    def __init__(self, fuzzer_id, submission_logger: logging.Logger, submission, problem_repository,
                 time_limit=TIMEOUT):
        threading.Thread.__init__(self)

        self.fuzzer_id = fuzzer_id
        self.submission_logger = submission_logger
        self.submission = submission
        self.submission["valid"] = True
        self.state = {'id': self.fuzzer_id, 'finished': False}
        self.time_limit = time_limit
        self.log_stream = StringIO()
        self.problem_repository: ProblemRepository = problem_repository

    def run(self):
        submission_logger = logging.getLogger(f"submission.{self.fuzzer_id}")
        submission_log_handler = logging.StreamHandler(self.log_stream)
        submission_log_handler.setFormatter(self.FORMATTER)
        submission_logger.addHandler(submission_log_handler)

        with tempfile.TemporaryDirectory(prefix="fuzzer") as d:
            directory = Path(d)

            output_directory = directory / "fail"
            output_directory.mkdir(parents=True)
            source_directory = directory / "source"
            source_directory.mkdir(parents=True)

            for name, source_code in self.submission["sources"].items():
                source_file = source_directory / name
                with source_file.open(mode="wt") as source:
                    source.write(source_code)

            try:
                self.submission_logger.debug("%s: Starting fuzzing", self.fuzzer_id)
                problem: RepositoryProblem = self.problem_repository.load_problem(self.submission["problem"])

                lang = languages.load_language_config()
                language = lang.languages.get(self.submission.get("lang"), None)
                if language is None:
                    language = lang.detect_language([str(path) for path in source_directory.iterdir()])
                self.submission_logger.info("Using language %s", language.name)

                Fuzzer(self.submission["secret_file"], source_directory, language, problem, output_directory,
                       self.submission.get("runs", 10), submission_logger, self.time_limit).run_random_case()
                self.submission_logger.debug("%s: Fuzzing finished.", self.fuzzer_id)

                # Find failing cases
                self.state['cases'] = FuzzingThread.read_results(output_directory)
            except MakeError as e:
                self.submission_logger.error("%s: Make rule %s failed with output:\n%s\n===\n%s\n",
                                             self.fuzzer_id, e.rule, e.out, e.err)
                submission_logger.error("Make failed")
            except ValueError as e:
                self.submission_logger.error("%s: Error during fuzzing: %s", self.fuzzer_id, e)
                submission_logger.error("%s", e)
            except Exception as e:
                self.submission_logger.warning("%s: Error during fuzzing", self.fuzzer_id, exc_info=e)
                submission_logger.error("Generic error during fuzzing: %s", e)

            self.state['log'] = self.log_stream.getvalue()
            submission_log_handler.flush()
            self.log_stream.close()
            self.state['finished'] = True

    def getstate(self):
        state = self.state.copy()
        if not state['finished']:
            state['log'] = self.log_stream.getvalue()
        return state
