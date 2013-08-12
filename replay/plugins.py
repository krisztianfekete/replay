import abc
import external_process
import shutil
import os
from replay import exceptions
import getpass
import datetime
import tempfile
from externals import fspath
import hashlib
import logging


log = logging.getLogger(__name__)


class Plugin(object):

    '''I am a context manager, I can perform setup tasks \
    and also provide cleanup for Runner

    My operation is usually driven by runner.context & runner.script
    '''

    __metaclass__ = abc.ABCMeta

    def __init__(self, context, script):
        self.context = context
        self.script = script

    @abc.abstractmethod
    def __enter__(self):  # pragma: nocover
        pass

    @abc.abstractmethod
    def __exit__(self, exc_type, exc_value, traceback):  # pragma: nocover
        pass

    def has_option(self, option):
        return self.script.has_option(option)


class _WorkingDirectoryPlugin(Plugin):

    '''I am a base class helping to implement real Plugins \
    that ensure that the scripts run in a clean directory, \
    and also clean up after them.
    '''

    def __init__(self, context, script):
        super(_WorkingDirectoryPlugin, self).__init__(context, script)
        self.original_working_directory = os.getcwd()
        self.working_directory = None

    def __exit__(self, exc_type, exc_value, traceback):
        log.debug('%s: __exit__', self.__class__.__name__)
        try:
            self._change_to_directory(self.original_working_directory)
        finally:
            log.debug(
                '%s: rmtree %s',
                self.__class__.__name__,
                self.working_directory)
            shutil.rmtree(self.working_directory)

    def _change_to_directory(self, directory):
        log.debug('%s: chdir %s', self.__class__.__name__, directory)
        os.chdir(directory)

    def _copy_scripts_to(self, destination):
        source = self.script.dir
        log.debug(
            '%s: copytree %s -> %s',
            self.__class__.__name__,
            source,
            destination)
        shutil.copytree(source, destination)


class WorkingDirectory(_WorkingDirectoryPlugin):

    '''I ensure that the scripts run in a clean directory, \
    and also clean up after them.

    The directory to run in is taken from the context.
    '''

    def __enter__(self):
        log.debug('WorkingDirectory: __enter__')
        log.debug('WorkingDirectory: copy script directory')
        self.working_directory = self.context.working_directory.path

        self._copy_scripts_to(self.working_directory)
        self._change_to_directory(self.working_directory)


class TemporaryDirectory(_WorkingDirectoryPlugin):

    '''I ensure that the scripts run in a clean directory, \
    and also clean up after them.

    The directory to run in is a new temporary directory.
    '''

    def __enter__(self):
        log.debug('TemporaryDirectory: __enter__')
        log.debug('TemporaryDirectory: copy script directory')
        self.working_directory = tempfile.mkdtemp()
        # real_working_directory is one directory further
        # the reason is: copytree requires a non-existing target
        # the choice here was between:
        # 1. add an extra directory
        # 2. reimplement a copytree to work with already existing target
        real_working_directory = os.path.join(self.working_directory, '-')

        self._copy_scripts_to(real_working_directory)
        self._change_to_directory(real_working_directory)


class DataStore(Plugin):

    '''I ensure that inputs are available from DataStore and outputs are saved.
    '''

    def __enter__(self):
        self._check_inputs()
        self._download_inputs()

    def __exit__(self, exc_type, exc_value, traceback):
        self._check_outputs()
        self._upload_outputs()

    # helpers
    def _file_pairs(self, copy_spec):
        datastore = self.context.datastore
        working_directory = fspath.working_directory()

        for spec in copy_spec:
            for local_file, ds_file in spec.iteritems():
                yield (working_directory / local_file), (datastore / ds_file)

    def _input_file_pairs(self):
        return self._file_pairs(self.script.inputs)

    def _output_file_pairs(self):
        return self._file_pairs(self.script.outputs)

    def _check_inputs(self):
        for local, datastore in self._input_file_pairs():
            if not datastore.exists():
                raise exceptions.MissingInput(datastore)

    def _check_outputs(self):
        for local, datastore in self._output_file_pairs():
            if not local.exists():
                raise exceptions.MissingOutput(local)

    def _download_inputs(self):
        for local, datastore in self._input_file_pairs():
            datastore.copy_to(local)

    def _upload_outputs(self):
        for local, datastore in self._output_file_pairs():
            local.copy_to(datastore)


class _EnvironKeyState(object):

    def __init__(self, environ, key):
        self.key = key
        self.missing = key not in environ
        self.value = environ.get(key)

    def restore(self, environ):
        key = self.key
        if self.missing:
            if key in environ:
                del environ[key]
        else:
            environ[key] = self.value


class PythonDependencies(Plugin):

    def __init__(self, context, script):
        super(PythonDependencies, self).__init__(context, script)
        self.virtualenv_name = '_replay_' + self._package_hash()
        self.virtualenv_dir = (
            self.context.virtualenv_parent_dir / self.virtualenv_name)
        self.PATH = _EnvironKeyState(os.environ, 'PATH')

    def __enter__(self):
        venv_bin = (self.virtualenv_dir / 'bin').path
        path = venv_bin + os.pathsep + os.environ.get('PATH', '')
        os.environ['PATH'] = path
        if not self.virtualenv_dir.exists():
            self._make_virtualenv()

    def __exit__(self, exc_type, exc_value, traceback):
        self.PATH.restore(os.environ)

    def _package_hash(self):
        python_dependencies = self.script.python_dependencies
        dependencies = '\n'.join(sorted(python_dependencies))
        return hashlib.md5(dependencies).hexdigest()

    @property
    def index_server_url(self):
        return self.context.index_server_url

    def _install_package(self, package_spec, index_server_url):
        cmdspec = (
            ['pip', 'install']
            + (['--index-url=' + index_server_url] if index_server_url else [])
            + [package_spec])

        result = external_process.run(cmdspec)
        if result.status != 0:
            raise exceptions.MissingPythonDependency(result)

    def _make_virtualenv(self):
        # potential enhancements:
        #  - clean environment from behavior changing settings
        #    (e.g. PYTHON_VIRTUALENV)
        #  - specify python interpreter to use (python 2 / 3 / pypy / ...)
        external_process.run(['virtualenv', self.virtualenv_dir.path])
        python_dependencies = self.script.python_dependencies
        for package_spec in python_dependencies:
            self._install_package(package_spec, self.index_server_url)


class Postgres(Plugin):

    def __init__(self, context, script):
        super(Postgres, self).__init__(context, script)
        self.timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')
        self.enabled = self.has_option('uses psql')
        self.keep_database = (
            self.has_option('debug') and self.has_option('keep database'))
        self.PGDATABASE = _EnvironKeyState(os.environ, 'PGDATABASE')

    @property
    def database(self):
        return '{user}_{script_name}_{timestamp}'.format(
            script_name=self.script.name,
            user=getpass.getuser(),
            timestamp=self.timestamp)

    def __enter__(self):
        if self.enabled:
            os.environ['PGDATABASE'] = self.database
            external_process.run(['createdb', self.database])

    def __exit__(self, exc_type, exc_value, traceback):
        self.PGDATABASE.restore(os.environ)
        if not self.keep_database:
            external_process.run(['dropdb', self.database])


class Execute(Plugin):

    def __enter__(self):
        if self.script.executable_name:
            command = ['python', self.script.executable_name]
            result = external_process.run(command)
            if result.status != 0:
                raise exceptions.ScriptError(result)

    def __exit__(self, exc_type, exc_value, traceback):
        pass
