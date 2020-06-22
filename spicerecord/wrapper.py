# This is a thin wrapper that invokes spice-record
# in a subprocess. This exists to help avoid any oddities
# of importing GLib into your program.
import sys
import subprocess
import tempfile

class SpiceRecordWrapper:
    def __init__(self, dom, output=None, uri=None):
        self.p = None
        self.stopped = False
        self.stderr_file = tempfile.NamedTemporaryFile(
                prefix='spice-record-stderr-', suffix='.txt')

        self.dom = dom
        self.output = output
        self.uri = uri

    def _raise_failure(self):
        self.stderr_file.seek(0)
        errtxt = self.stderr_file.read().decode()
        raise Exception("spice record failed ({}):\n{}".format(self.p.returncode, errtxt))

    def __enter__(self):
        pkg = '.'.join(__name__.split('.')[:-1])
        args = [
            sys.executable, '-m', pkg,
            '--quiet',
        ]
        if self.output:
            args += ['--output', self.output]
        if self.uri:
            args += ['--connect', self.uri]
        args.append(self.dom)

        self.p = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self.stderr_file,
                )

        # Briefly wait to make sure it didn't bomb early
        try:
            self.p.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        else:
            self._raise_failure()

        return self

    def __exit__(self, *exc_info):
        if not self.stopped:
            self.stop()
        self.wait()
        self.stderr_file.close()

    def stop(self):
        if self.stopped:
            raise Exception("stop() already called")
        try:
            self.p.stdin.write(b'Q\n')
            self.p.stdin.close()
        except (BrokenPipeError, IOError):
            # The process has already exited
            pass
        self.stopped = True

    def wait(self):
        if not self.stopped:
            raise Exception("stop() not yet called")
        if not self.p:
            return
        if self.p.wait() != 0:
            self._raise_failure()
        self.p = None
