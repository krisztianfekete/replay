import os
from externals import fspath
from replay import exceptions
import external_process


class Context(object):

    datastore = None  # External
    virtualenv_parent_dir = str

    def __init__(self, datastore=None, virtualenv_parent_dir=None):
        wd = fspath.working_directory()
        self.datastore = datastore or wd
        if virtualenv_parent_dir:
            self.virtualenv_parent_dir = virtualenv_parent_dir
        else:
            self.virtualenv_parent_dir = wd / '.virtualenvs'


class Runner(object):

    '''I run scripts in isolation'''

    def __init__(self, context, script):
        self.context = context
        self.script = script
        self.virtualenv_name = '_replay_venv'

    @property
    def virtualenv_dir(self):
        return self.context.virtualenv_parent_dir / self.virtualenv_name

    def check_inputs(self):
        datastore = self.context.datastore
        for input_spec in self.script.inputs:
            ds_file, = input_spec.values()
            if not (datastore / ds_file).is_file():
                raise exceptions.MissingInput(ds_file)

    def run_in_virtualenv(self, cmdspec):
        venv_bin = (self.virtualenv_dir / 'bin').path
        path = venv_bin + os.pathsep + os.environ.get('PATH', '')
        env = dict(os.environ, PATH=path)
        return external_process.run(cmdspec, env=env)

    def install_package(self, package_spec, index_server_url):
        cmdspec = (
            ['pip', 'install']
            + (['--index-url=' + index_server_url] if index_server_url else [])
            + [package_spec])
        result = self.run_in_virtualenv(cmdspec)
        if result.status != 0:
            raise exceptions.MissingPythonDependency(result)

    def make_virtualenv(self, index_server_url=None):
        # potential enhancements:
        #  - clean environment from behavior changing settings
        #    (e.g. PYTHON_VIRTUALENV)
        #  - specify python interpreter to use (python 2 / 3 / pypy / ...)
        if self.virtualenv_dir.is_dir():
            return

        external_process.run(['virtualenv', self.virtualenv_dir.path])
        for package_spec in self.script.python_dependencies:
            self.install_package(package_spec, index_server_url)
