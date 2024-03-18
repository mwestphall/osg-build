"""utilities for osg-build"""
import configparser
import contextlib
import errno
from itertools import zip_longest
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, AnyStr, Iterable, List, Union
from datetime import datetime

from . import constants
from . import error

log = logging.getLogger(__name__)


def to_str(strlike, encoding="latin-1", errors="backslashreplace"):
    """Decodes a bytes into a str Python 3; runs str() on other types"""
    if isinstance(strlike, bytes):
        return strlike.decode(encoding, errors)
    else:
        return str(strlike)


def maybe_to_str(strlike, encoding="latin-1", errors="backslashreplace"):
    """Decodes a bytes into a str; leaves other types alone"""
    if isinstance(strlike, bytes):
        return strlike.decode(encoding, errors)
    else:
        return strlike


class CalledProcessError(Exception):
    """Returned by checked_call and checked_backtick if the subprocess exits
    nonzero.

    """
    def __init__(self, process, returncode, output=None):
        # This breaks in python 2.4 (because Exception isn't a new-style class?)
        # super(CalledProcessError, self).__init__()
        Exception.__init__(self)
        self.process = process
        self.returncode = returncode
        self.output = output

    def __str__(self):
        log.debug(self.output)
        return ("Error in called process(%s): subprocess returned %s.\nOutput: %s" %
                (str(self.process), str(self.returncode), str(self.output)))

    def __repr__(self):
        return str((repr(self.process),
                    repr(self.returncode),
                    repr(self.output)))


# pipes.quote was deprecated in Python 2.7, but its replacement, shlex.quote
# was not added until Python 3.3
try:
    shell_quote = shlex.quote
except AttributeError:
    from pipes import quote as shell_quote


class IniConfiguration:
    def __init__(self,
                 inifiles: Union[str, Iterable[str]],
                 parser_class=configparser.RawConfigParser):
        if not inifiles:
            raise ValueError("At least one inifile must be provided")

        self.cp = parser_class()
        self.cp.read(inifiles)
        if not self.cp.sections:
            raise error.Error("No configuration could be loaded")

    def config_safe_get(self, section: str, option: str, default=None) -> Any:
        """Read an option from a config file, returning the default value
        if the option or section is missing.
        """
        try:
            return self.cp.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def config_safe_get_list(self, section: str, option: str) -> List[str]:
        """Read an option from a config file and parse it as a comma-or-whitespace-
        separated list, returning the empty list value if the option or section
        is missing.
        """
        return self._parse_list_str(self.config_safe_get(section, option, ""))

    @staticmethod
    def _parse_list_str(list_str: str) -> List[str]:
        # split string on whitespace or commas, removing empty items
        return list(filter(None, re.split(r'[ ,\t\n]', list_str)))


def checked_call(*args, **kwargs):
    """A wrapper around subprocess.call() that raises CalledProcessError on
    a nonzero return code. Similar to subprocess.check_call() in 2.7+, but
    prints the command to run and the result if loglevel is DEBUG.

    """
    err = unchecked_call(*args, **kwargs)
    if err:
        raise CalledProcessError([args, kwargs], err, None)


def unchecked_call(*args, **kwargs):
    """A wrapper around subprocess.call() with the same semantics as checked_call: 
    Prints the command to run and the result if loglevel is DEBUG.

    """
    log.debug("Running %r", args)

    err = subprocess.call(*args, **kwargs)
    log.debug("Subprocess returned " + str(err))
    return err


def checked_pipeline(cmds, stdin=None, stdout=None, **kw):
    """Run a list of commands pipelined together, raises CalledProcessError if
    any have a nonzero return code.

    Each item in cmds is interpreted as a cmd argument for subprocess.Popen
    stdin  (optional) applies only to cmd[0]
    stdout (optional) applies only to cmd[-1]
    any additional kw args apply to all cmds

    Prints the commands to run and the results if loglevel is DEBUG.
    """
    err = unchecked_pipeline(cmds, stdin, stdout, **kw)
    if err:
        raise CalledProcessError([cmds, kw], err, None)


def unchecked_pipeline(cmds, stdin=None, stdout=None, **kw):
    """Run a list of commands pipelined together, returns zero if all succeed,
    otherwise the first nonzero return code if any fail.

    Argument semantics are the same as checked_pipeline

    Prints the commands to run and the results if loglevel is DEBUG.
    """
    log.debug("Running %s" % ' | '.join(map(str, cmds)))
    pipes = []
    final = len(cmds) - 1
    for i, cmd in enumerate(cmds):
        _stdin  = stdin  if i == 0     else pipes[-1].stdout
        _stdout = stdout if i == final else subprocess.PIPE
        pipes.append(subprocess.Popen(cmd, stdin=_stdin, stdout=_stdout, **kw))
        if i > 0:
            pipes[-2].stdout.close()
            pipes[-2].stdout = None
    rets = [ p.wait() for p in pipes ]
    log.debug("Subprocesses returned (%s)" % ','.join(map(str, rets)))
    return list(filter(None, rets))[0] if any(rets) else 0


def backtick(*args, **kwargs):
    """Call a process and return its output, ignoring return code.
    See checked_backtick() for semantics.

    """
    try:
        output = checked_backtick(*args, **kwargs)
    except CalledProcessError as e:
        output = e.output

    return output


def sbacktick(*args, **kwargs):
    """Call a process and return a pair containing its output and exit status.
    See checked_backtick() for semantics.

    """
    returncode = 0
    try:
        output = checked_backtick(*args, **kwargs)
    except CalledProcessError as e:
        output = e.output
        returncode = e.returncode

    return (output, returncode)


def checked_backtick(*args, **kwargs):
    """Call a process and return a string containing its output.
    This is a wrapper around subprocess.Popen() and passes through arguments
    to subprocess.Popen().

    Raises CalledProcessError if the process has a nonzero exit code. The
    'output' field of the CalledProcessError contains the output in that case.

    If the command is a string and 'shell' isn't passed, it's split up
    according to shell quoting rules using shlex.split()

    The output is stripped unless nostrip=True is specified.
    If err2out=True is specified, stderr will be included in the output.

    Unless clocale=False is specified, LC_ALL=C and LANG=C will be added to the
    subprocess's environment, forcing the 'C' locale for program output.

    """
    cmd = args[0]
    if isinstance(cmd, str) and 'shell' not in kwargs:
        cmd = shlex.split(cmd)

    sp_kwargs = kwargs.copy()

    nostrip = sp_kwargs.pop('nostrip', False)
    sp_kwargs['stdout'] = subprocess.PIPE
    if sp_kwargs.pop('err2out', False):
        sp_kwargs['stderr'] = subprocess.STDOUT
    if sp_kwargs.pop('clocale', True):
        sp_kwargs['env'] = dict(sp_kwargs.pop('env', os.environ), LC_ALL='C', LANG='C')

    log.debug("Running `%s`" % cmd)
    proc = subprocess.Popen(cmd, *args[1:], **sp_kwargs)

    output = maybe_to_str(proc.communicate()[0])
    if not nostrip:
        output = output.strip()
    err = proc.returncode
    log.debug("Subprocess returned " + str(err))

    if err:
        raise CalledProcessError([args, kwargs], err, output)
    else:
        return output


def slurp(filename):
    """Return the contents of a file as a single string."""
    with open(filename, 'r') as fh:
        contents = fh.read()
    return contents


def unslurp(filename, contents):
    """Write a string to a file."""
    with open(filename, 'w') as fh:
        fh.write(contents)


def atomic_unslurp(filename, contents, mode=0o644):
    """Write contents to a file, making sure a half-written file is never
    left behind in case of error.

    """
    fd, tempname = tempfile.mkstemp(dir=os.path.dirname(filename))
    try:
        try:
            os.write(fd, contents)
        finally:
            os.close(fd)
    except EnvironmentError:
        os.unlink(tempname)
        raise
    os.rename(tempname, filename)
    os.chmod(filename, mode)


def find_file(filename, paths=None, strict=False):
    """Go through each directory in paths and look for filename in it. Return
    the first match.

    """
    matches = find_files(filename, paths, strict)
    if matches:
        return matches[0]
    else:
        return None


def find_files(filename, paths=None, strict=False):
    """Go through each directory in paths and look for filename in it. Return
    all matches.

    """
    matches = []
    if paths is None:
        paths = constants.DATA_FILE_SEARCH_PATH
    for p in paths:
        j = os.path.join(p, filename)
        if os.path.isfile(j):
            matches += [j]
    if not matches and strict:
        raise error.FileNotFoundInSearchPathError(filename, paths)
    return matches


def super_unpack(*compressed_files):
    """Extracts compressed files, calling the appropriate expansion
    program based on the file extension."""

    handlers = [
        ('.tar.bz2',  'tar xjf %s'),
        ('.tar.gz',   'tar xzf %s'),
        ('.bz2',      'bunzip2 %s'),
        ('.rar',      'unrar x %s'),
        ('.gz',       'gunzip %s'),
        ('.tar',      'tar xf %s'),
        ('.tbz2',     'tar xjf %s'),
        ('.tgz',      'tar xzf %s'),
        ('.zip',      'unzip %s'),
        ('.Z',        'uncompress %s'),
        ('.7z',       '7z x %s'),
        ('.tar.xz',   'xz -d %s -c | tar xf -'),
        ('.xz',       'xz -d %s'),
        ('.rpm',      'rpm2cpio %s | cpio -id'),
    ]
    for cf in compressed_files:
        for (ext, cmd) in handlers:
            if cf.endswith(ext):
                subprocess.call(cmd % shell_quote(cf), shell=True)
                break


def safe_makedirs(directory, mode=0o777):
    """Create a directory and all its parent directories, unless it already
    exists.

    """
    if not os.path.isdir(directory):
        os.makedirs(directory, mode)


def ask(question: str, choices: Iterable[str], default=None) -> str:
    """Prompt user for a choice from a list. Return the choice.
    Return `default` if it's set and the user doesn't enter anything.
    """
    choices_lc = [x.lower() for x in choices]
    user_choice = ""
    match = False
    while not match:
        print(question)
        user_choice = input("[" + "/".join(choices) + "] ? ").strip().lower()
        if not user_choice and default is not None:
            return default
        for choice in choices_lc:
            if user_choice.startswith(choice) or choice.startswith(user_choice):
                match = True
                break
    return user_choice


def ask_yn(question):
    """Prompt user for a yes/no question. Return True or False for yes or no"""
    user_choice = ask(question, ("y", "n"))
    if user_choice.startswith("y"):
        return True
    else:
        return False


def safe_make_backup(filename, move=True, simple_suffix=False):
    """Back up a file if it exists (either copy or move)"""
    if simple_suffix:
        suffix = ".bak"
    else:
        suffix = datetime.now().strftime(".%y%m%d%H%M%S~")
    newname = filename + suffix
    try:
        if move:
            os.rename(filename, newname)
        else:
            shutil.copy(filename, newname)
    except EnvironmentError as err:
        if err.errno == errno.ENOENT:  # no file to back up
            pass
        elif "are the same file" in str(err):  # file already backed up
            pass
        else:
            raise


# original from rsvprobe.py by Marco Mambelli
def which(program):
    """Python replacement for which"""
    def is_exe(f_path):
        """is a regular file and is executable"""
        return os.path.isfile(f_path) and os.access(f_path, os.X_OK)
    fpath, _ = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


def printf(fstring: AnyStr, *args, **kwargs):
    """A shorthand for printing with a format string.
    The kwargs 'file' and 'end' are as in the Python3 print function.
    """
    file_ = kwargs.pop('file', sys.stdout)
    end = kwargs.pop('end', "\n")
    ffstring = to_str(fstring) + to_str(end)
    if len(args) == 0 and len(kwargs) > 0:
        file_.write(ffstring % kwargs)
    elif len(args) == 1 and type(args[0]) == dict:
        file_.write(ffstring % args[0])
    else:
        file_.write(ffstring % args)


def errprintf(fstring: AnyStr, *args, **kwargs):
    """printf to stderr"""
    kwargs.pop('file', None)
    printf(fstring, file=sys.stderr, *args, **kwargs)


class safelist(list):
    """A version of the list type that has get and pop methods that accept
    default arguments instead of raising errors. (Compare dict.get and dict.pop)
    """
    def get(self, idx, default=None):
        """L.get(idx, default=None) -> item
        Get item at idx. If idx is out of range, return default."""
        try:
            return self.__getitem__(idx)
        except IndexError:
            return default

    def pop(self, *args):
        """L.pop([idx[,default]]) -> item, remove specified index
        (default last). If idx is out of range, then if return default if
        specified, raise IndexError if not.
        """
        try:
            return list.pop(self, args[0])
        except IndexError:
            if len(args) < 2:
                raise
            else:
                return args[1]


def get_screen_columns():
    # type: () -> int
    """Return the number of columns in the screen"""
    default = 80
    try:
        columns = int(os.environ.get('COLUMNS', backtick("stty size").split()[1]))
        if columns < 10:
            columns = default
    except (TypeError, OSError):
        columns = default
    return columns


def print_line(file=None):
    """Print a line the width of the screen (minus 1) so it doesn't cause an
    extra line break

    """
    if not file:
        file = sys.stdout
    print("-" * (get_screen_columns() - 1), file=file)


def print_table(columns_by_header):
    """Print a dict of lists in a table, with each list being a column"""
    screen_columns = get_screen_columns()
    field_width = int(screen_columns / len(columns_by_header))
    columns = []
    for entry in sorted(columns_by_header):
        columns.append([entry, '-' * len(entry)] + sorted(columns_by_header[entry]))
    for columns_in_row in zip_longest(fillvalue='', *columns):
        for col in columns_in_row:
            printf("%-*s", field_width - 1, col, end=' ')
        printf("")


def is_url(location):
    return re.match(r'[-a-z+]+://', to_str(location))


# Functions for manipulating a directory stack in the style of bash
# pushd/popd.
__dir_stack = []


def pushd(new_dir):
    """Change the current working directory to `new_dir`, and push the
    old one onto the directory stack `__dir_stack`.
    """
    global __dir_stack

    old_dir = os.getcwd()
    os.chdir(new_dir)
    __dir_stack.append(old_dir)


def popd():
    """Change to the topmost directory in the directory stack
    `__dir_stack` and pop the stack.  Note: the stack will be
    popped even if the chdir fails.

    Raise `IndexError` if the stack is empty.
    """
    global __dir_stack

    try:
        os.chdir(__dir_stack.pop())
    except IndexError:
        raise IndexError("Directory stack empty")


def get_local_machine_dver():
    # type: () -> str
    """Return the distro version (e.g. 'el6', 'el7') of the local machine
    or the empty string if we can't figure it out."""
    try:
        os_release_contents = slurp("/etc/os-release")
        if not os_release_contents:
            return ""  # empty file?
    except EnvironmentError:  # some error reading the file
        return ""

    os_release = {}
    for line in os_release_contents.split("\n"):
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')

    version_id = os_release.get("VERSION_ID", "")
    if not version_id:
        # dunno, bailing
        return ""

    major_version = version_id.split(".")[0]
    if "rhel" in os_release.get("ID_LIKE", ""):
        return "el%s" % major_version
    elif "fedora" == os_release.get("ID"):
        return "fc%s" % major_version
    else:
        return ""


def get_local_machine_release():
    # type: () -> int
    """Return the distro version (e.g. 6, 7) of the local machine
    or 0 if we can't figure it out."""
    dver = get_local_machine_dver()
    try:
        return int(re.search(r"\d+", dver).group(0))
    except AttributeError:  # no match
        return 0


def comma_join(iterable):
    # type: (Iterable) -> str
    """Returns the iterable sorted and joined with ', '"""
    return ", ".join(str(x) for x in sorted(iterable))


@contextlib.contextmanager
def chdir(directory):
    olddir = os.getcwd()
    os.chdir(directory)
    yield
    os.chdir(olddir)


def split_nvr(build):
    """Split an NVR into a (Name, Version, Release) tuple"""
    match = re.match(r"(?P<name>.+)-(?P<version>[^-]+)-(?P<release>[^-]+)$", build)
    if match:
        return match.group('name'), match.group('version'), match.group('release')
    else:
        return '', '', ''
