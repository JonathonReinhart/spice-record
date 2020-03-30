# This is a thin wrapper that invokes spice-record
# in a subprocess. This exists to help avoid any oddities
# of importing GLib into your program.
import sys
import subprocess

class SpiceRecordWrapper:
    def __init__(self, dom, output=None):
        self.p = None
        self.stopped = False

        self.dom = dom
        self.output = output
        self.uri = uri

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
                stderr=subprocess.DEVNULL,
                )
        return self

    def __exit__(self, *exc_info):
        if not self.stopped:
            self.stop()
        self.wait()

    def stop(self):
        if self.stopped:
            raise Exception("stop() already called")
        self.p.stdin.write(b'Q\n')
        self.p.stdin.close()
        self.stopped = True

    def wait(self):
        if not self.stopped:
            raise Exception("stop() not yet called")
        if not self.p:
            return
        self.p.wait()
        self.p = None
