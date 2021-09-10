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

from problemtools.problemtools import languages
from problemtools.problemtools.verifyproblem import ProblemAspect, Problem, TestCaseGroup, re_argument, default_args
from problemtools.problemtools.run import SourceCode


class Picker:
    def __init__(self, layout_file: Path):
        with layout_file.open(mode="rt") as f:
            empty = sum(not line.strip() for line in f)
            f.seek(0)
            cases = int(f.readline())
            if cases < 3:
                raise ValueError("Given test case does not have at least 3 cases")
            if empty not in [0, 1, cases - 1, cases]:
                raise ValueError("could not deduce testcase layout")
            self.preamble = False
            self.single_line = False
            if empty == 1 or empty == cases:
                self.preamble = True
            if empty < 2:
                self.single_line = True

    def split_case(self, case_file: Path, first):
        with case_file.open(mode="rt") as f:
            cases = int(f.readline())
            half = cases // 2

            if first:
                ret = [str(half) + "\n"]
            else:
                ret = [str(cases - half) + "\n"]

            if self.preamble:
                # read and save preamble
                ret = ret + self._read_to_empty(f) + ["\n"]

            for i in range(cases):
                if (i < half) == first:
                    ret = ret + self._read_case(f)
                    if not self.single_line:
                        ret += ["\n"]
                else:
                    self._read_case(f)

            if not self.single_line and len(ret) > 1:
                # remove last empty line
                ret.pop()
            return ret

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
    def _read_to_empty(f):
        res = []
        while line := f.readline().strip():
            res.append(line)
        return res

    def _read_case(self, f):
        if self.single_line:
            return [f.readline()]
        return self._read_to_empty(f)

    def pick_case(self, case_file, case_number):
        with case_file.open(mode="rt") as f:
            cases = int(f.readline())
            if case_number < 1 or case_number > cases:
                raise ValueError(f"invalid case number {case_number}")

            ret = ["1\n"]
            if self.preamble:
                ret += self._read_to_empty(f) + ["\n"]
            for _ in range(case_number - 1):
                self._read_case(f)
            ret = ret + self._read_case(f)
            return ret


class Fuzzer:
    RANDOMIZED_CASES = 500
    MAX_FAILS = 3

    @staticmethod
    def parse_feedback(result):
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
    def randomize(original, randomized, cases, seed):
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

    def run_make(self, rule: Union[str, Path]):
        self.logger.debug("Make %s", rule)
        if isinstance(rule, Path):
            rule = rule.relative_to(self.problem_directory)
        process = subprocess.Popen(["make", rule], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding="utf-8", cwd=str(self.problem_directory))
        out, err = process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"make {rule} failed\n===\n{out}\n===\n{err}")

    def copy_data(self, data_dir: Path, tmpdir: Path, seed_file: Path, input_file: Path, answer_file: Path,
                  feedback: Dict[str, List[str]]):
        shutil.copy(seed_file, data_dir / "case.seed")
        shutil.copy(input_file, data_dir / "case.in")
        shutil.copy(answer_file, data_dir / "case.ans")

        output = tmpdir / "output"
        if os.path.isfile(output):
            shutil.copy(output, data_dir / "case.out")
        for name, content in feedback.items():
            with (data_dir / name).open(mode="wt") as f:
                f.writelines(content)

    def __init__(self, case: str, source_directory: pathlib.Path, problem_directory: pathlib.Path,
                 output_directory: pathlib.Path, logger, time_limit):
        self.case = case
        self.source_directory = source_directory.absolute()
        self.problem_directory = problem_directory.absolute()
        self.output_directory = output_directory.absolute()
        self.logger = logger
        self.time_limit = time_limit

        self.alternate_limit = False

    def run_submission(self, test_data, program, args):
        # Trick the cache key ...
        time_limit_high = self.time_limit * 2 + (0.001 if self.alternate_limit else 0.0)
        self.alternate_limit = not self.alternate_limit
        return test_data.run_submission(program, args, self.time_limit, time_limit_high)

    def run_random_case(self, language_key):
        args = default_args()
        args.bail_on_error = False
        args.parts = ["submissions"]
        args.problemdir = self.problem_directory
        ProblemAspect.silent = True

        self.run_make("checker")

        seed_file = None
        input_file = None
        answer_file = None
        with Problem(self.problem_directory) as problem:
            try:
                problem_data_directory = self.problem_directory / 'data'
                secret_directory = problem_data_directory / 'secret'
                case_file = secret_directory / (self.case + '.seed')

                if not case_file.is_file():
                    raise ValueError(f"Could not locate seed file {case_file}")

                fail_path_wa = self.output_directory / "wa"
                fail_path_wa.mkdir(parents=True)
                fail_path_rte = self.output_directory / "rte"
                fail_path_rte.mkdir(parents=True)

                lang = languages.load_language_config()
                language = None
                if language_key is not None:
                    language = lang.languages.get(language_key, None)
                if language is None:
                    language = lang.detect_language([str(path) for path in self.source_directory.iterdir()])
                self.logger.info("Detected language %s", language.name)
                program = SourceCode(path=str(self.source_directory.absolute()),
                                     language=language, work_dir=problem.tmpdir)

                self.logger.info("Compiling program %s", program.name)
                (compilation_result, error) = program.compile()
                if not compilation_result:
                    raise ValueError(f'Compile error for program {program.name}: {error}')

                self.run_make("generators")

                failed_wa = 0
                failed_rte = 0
                runs = 100
                picker = None

                for i in range(runs):
                    seed = str(random.getrandbits(63))
                    seed_file = secret_directory / f"{self.case}_{seed}.seed"
                    input_file = secret_directory / f"{self.case}_{seed}.in"
                    answer_file = secret_directory / f"{self.case}_{seed}.ans"

                    self.logger.debug("Randomizing case %s", self.case)

                    self.randomize(case_file, seed_file, Fuzzer.RANDOMIZED_CASES, seed)
                    self.run_make(input_file)
                    self.run_make(answer_file)

                    if picker is None:
                        picker = Picker(input_file)

                    test_data = TestCaseGroup(problem, problem_data_directory)

                    args.data_filter = re_argument(self.case + "_" + seed)
                    (result1, result2) = self.run_submission(test_data, program, args)

                    verdict = str(result1)[:2]
                    if verdict == 'WA':
                        self.logger.debug("found problematic input, picking failing case")

                        feedback_files = Fuzzer.parse_feedback(result1)
                        failing_case = Picker.first_failing_case(feedback_files["judgemessage.txt"], answer_file)
                        picked_case = picker.pick_case(input_file, failing_case)
                        with input_file.open(mode="wt") as f:
                            for line in picked_case:
                                f.write(line)

                        self.run_make(answer_file)

                        self.logger.debug("running program again on singular case")
                        (result1, result2) = self.run_submission(test_data, program, args)

                        failed_wa += 1
                        run_data_dir = fail_path_wa / f"fail_{failed_wa}"
                        run_data_dir.mkdir()
                        self.copy_data(run_data_dir, Path(problem.tmpdir), seed_file, input_file, answer_file,
                                       feedback_files)
                        if str(result1)[:2] != 'WA':
                            self.logger.info("Program has feedback errors between test cases"
                                             "(or outputs something after the correct answer)")
                            return
                    elif verdict == 'TL':
                        self.logger.info("Program hit time limit")
                        return
                    elif verdict == 'JE':
                        self.logger.info("Judge Error occurred")
                        return
                    elif verdict == 'RT':
                        # binary search for the error
                        self.logger.debug("Runtime error occurred, binary search for the test case")

                        first_half = picker.split_case(input_file, True)
                        second_half = picker.split_case(input_file, False)

                        while first_half[0] != "0\n":
                            with input_file.open(mode='wt') as f:
                                f.writelines(first_half)
                            self.run_make(answer_file)

                            self.logger.debug("running program again on half of remainder")
                            (result1, result2) = self.run_submission(test_data, program, args)
                            if str(result1)[:2] == 'RT':
                                self.logger.debug("RTE occurred in first half")
                            else:
                                self.logger.debug("RTE occurred in second half")
                                with input_file.open(mode='wt') as f:
                                    f.writelines(second_half)

                            first_half = picker.split_case(input_file, True)
                            second_half = picker.split_case(input_file, False)

                        self.logger.debug("should have RTE case now")
                        with input_file.open(mode='wt') as f:
                            f.writelines(second_half)
                        self.run_make(answer_file)

                        self.logger.debug("Running program on RTE case")
                        (result1, result2) = self.run_submission(test_data, program, args)

                        failed_rte += 1
                        run_data_dir = fail_path_rte / f"fail_{failed_rte}"
                        run_data_dir.mkdir()
                        feedback_files = Fuzzer.parse_feedback(result1)
                        self.copy_data(run_data_dir, Path(problem.tmpdir), seed_file, input_file, answer_file,
                                       feedback_files)
                        if str(result1)[:2] == 'RT':
                            self.logger.debug("RTE binary search successful")
                        else:
                            self.logger.info("RTE binary search unsuccessful, program has feedback errors")
                            return

                    self.logger.info("finished %d runs of %d on %s (%d failed)",
                                     i + 1, runs, self.case, failed_wa + failed_rte)

                    for file in [input_file, seed_file, answer_file]:
                        file.unlink(missing_ok=True)

                    if failed_wa + failed_rte >= Fuzzer.MAX_FAILS:
                        self.logger.info("enough runs failed, ending run")
                        return

            finally:
                for file in [input_file, seed_file, answer_file]:
                    if file is not None:
                        try:
                            file.unlink(missing_ok=True)
                        except IOError:
                            pass
                self.run_make("clean")


#
# def single_file(path: Path, submission: Dict[str, Any], logger: logging.Logger):
#     if not submission["valid"]:
#         return
#     if len(submission["sources"]) == 1:
#         submission["main_file"] = path / next(iter(submission["sources"]))
#     else:
#         submission["valid"] = False
#         logger.error("More than one file.")
# 
# 
# def create_jar(path: Path, submission: Dict[str, Any], logger: logging.Logger):
#     if not submission["valid"]:
#         return
#     if submission["lang"] != "java":
#         return
# 
#     logger.debug("Building executable jar file.")
# 
#     build_path = path / "classes"
#     build_path.mkdir(parents=True)
#     build_command = ["javac", "-d", build_path]
#     source_files = [build_path / f for f in submission["sources"].keys()]
#     build_command.extend(source_files)
# 
#     logger.debug("Compiling submission")
#     p = Popen(build_command, stdout=PIPE, stderr=PIPE)
#     std_out, std_err = p.communicate()
#     if p.returncode != 0:
#         submission["valid"] = False
#         logger.error("Compile error: %s", std_err.strip())
#         return
# 
#     # Detecting main class
#     logger.debug("Detecting Main File")
#     p = Popen(["java", "-jar", "detectmain.jar", build_path], stdout=PIPE, stderr=PIPE)
#     std_out, std_err = p.communicate()
#     main_class = std_out
#     if p.returncode != 0 or main_class == "":
#         submission["valid"] = False
#         logger.error("Main File Detection error: %s", std_err.strip())
#         return
# 
#     logger.debug("Detected the main class to be %s", main_class)
# 
#     # Building jar file
#     logger.debug("Packaging jar file")
#     jar_command = ["jar", "cvfe0", build_path / "main.jar", main_class, "-C", build_path, "."]
#     p = Popen(jar_command, stdout=PIPE, stderr=PIPE)
#     std_out, std_err = p.communicate()
#     if p.returncode != 0:
#         submission["valid"] = False
#         logger.error("Packaging error: %s", std_err.strip())
#         return
# 
#     submission["sources"].clear()
#     submission["sources"]["main.jar"] = ""


class FuzzingThread(threading.Thread):
    TIMEOUT = 5
    FORMATTER = logging.Formatter("%(asctime)s - %(message)s")

    @staticmethod
    def read_results(output_directory, category):
        category_dir = output_directory / category
        if not category_dir.exists():
            return {}
        result = defaultdict(dict)
        for case_dir in category_dir.iterdir():
            if case_dir.is_dir():
                for case in case_dir.iterdir():
                    with case.open(mode="rt") as file:
                        result[case_dir.name][case.name] = file.read()
        return result

    def __init__(self, fuzzer_id, logger: logging.Logger, submission, cases, problem_repository, time_limit=TIMEOUT):
        threading.Thread.__init__(self)

        self.fuzzer_id = fuzzer_id
        self.logger = logger
        self.submission = submission
        self.submission["valid"] = True
        self.state = {'id': self.fuzzer_id, 'finished': False}
        self.cases = cases
        self.time_limit = time_limit
        self.log_stream = StringIO()
        self.problem_repository = problem_repository

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
                self.logger.debug('%s: Starting fuzzing', self.fuzzer_id)
                problem_directory = self.problem_repository / self.submission["problem"]
                Fuzzer(self.submission["secret_file"], source_directory, problem_directory, output_directory,
                       submission_logger, self.time_limit).run_random_case(self.submission.get("lang", None))
                self.logger.debug('%s: Fuzzing finished.', self.fuzzer_id)

                # Find failing cases
                self.state['cases'] = {
                    "wa": FuzzingThread.read_results(output_directory, "wa"),
                    "rte": FuzzingThread.read_results(output_directory, "rte")
                }
            except Exception as e:
                self.logger.warning("%s: Error during fuzzing.", self.fuzzer_id, exc_info=e)
                submission_logger.info("Error during fuzzing.")

            self.state['log'] = self.log_stream.getvalue()
            submission_log_handler.flush()
            self.log_stream.close()
            self.state['finished'] = True

    def getstate(self):
        state = self.state.copy()
        if not state['finished']:
            state['log'] = self.log_stream.getvalue()
        return state
