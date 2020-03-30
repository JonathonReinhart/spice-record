import argparse
import libvirt
import logging
import os
import shutil
import sys
import time
from uuid import UUID

from .version import __version__
from . import record


class AppError(Exception):
    def __init__(self, message, exit_code=2):
        super().__init__(message)
        self.exit_code = exit_code

def libvirt_err_handler(ignore, err):
    if err[3] != libvirt.VIR_ERR_ERROR:
        # Don't log libvirt errors: global error handler will do that
        logging.warn("Non-error from libvirt: '%s'" % err[2])


def lookup_domain(conn, key):
    try:
        # Try ID
        try:
            return conn.lookupByID(int(key))
        except ValueError:
            pass

        # Try UUID
        try:
            return conn.lookupByUUID(UUID(key).bytes)
        except ValueError:
            pass

        # Try name
        return conn.lookupByName(key)

    except libvirt.libvirtError as err:
        if err.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
        raise AppError(str(err))


def unique_filename(path):
    base, ext = os.path.splitext(path)
    idx = None

    while True:
        if idx is not None:
            path = "{}_{}{}".format(base, idx, ext)
        else:
            path = base + ext

        try:
            f = open(path, 'x')
            f.close()
            return path
        except FileExistsError:
            idx = 0 if (idx is None) else (idx + 1)




def parse_args():
    ap = argparse.ArgumentParser()

    # Recording options
    ap.add_argument('--vcodec', default='libx264',
            help='Set the output video codec (see "ffmpeg -encoders" for choices)')
    ap.add_argument('--loglevel', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
            help='Set the logging level (default=WARNING)', default='WARNING')
    ap.add_argument('-r', '--framerate', type=int, default=24)

    # Libvirt options and domain
    ap.add_argument('-c', '--connect', dest='libvirt_uri',
            help='Connect to hypervisor (e.g. qemu:///system)')
    ap.add_argument('machine', metavar='DOMAIN-NAME|ID|UUID',
            help='Machine to record')

    ap.add_argument('-o', '--output', metavar='FILENAME',
            help='Output filename (defaults to <domain-name>.mp4)')
    ap.add_argument('-q', '--quiet', action='store_true',
            help="Don't output anything to the console")
    ap.add_argument('-v', '--version', action='version',
            version='spice-record ' + __version__)

    return ap.parse_args()


def _main():
    args = parse_args()
    logging.basicConfig(level=args.loglevel)

    if args.quiet:
        record.quiet = True

    libvirt.registerErrorHandler(f=libvirt_err_handler, ctx=None)

    # Open libvirt connection
    logging.info("Opening connection to %s", args.libvirt_uri)
    conn = libvirt.open(args.libvirt_uri)   # read only access prevents virDomainOpenGraphicsFD

    # Try to get domain
    dom = lookup_domain(conn, args.machine)
    logging.info('Using domain "%s" (%s)', dom.name(), dom.UUIDString())

    if not args.output:
        args.output = unique_filename(dom.name() + '.mp4')

    record.record(args, dom)


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("Exiting due to Ctrl+C", file=sys.stderr)
        sys.exit(1)
    except AppError as err:
        print(err, file=sys.stderr)
        sys.exit(err.exit_code)
