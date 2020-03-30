#!/usr/bin/env python3
#
# References:
# - https://github.com/virt-manager/virt-manager/blob/master/virtManager/viewers.py
# - http://lazka.github.io/pgi-docs/#SpiceClientGLib-2.0
# - spice-gtk/src/spicy-screenshot.c
# - https://linuxtv.org/downloads/v4l-dvb-apis/uapi/v4l/pixfmt-packed-rgb.html
# - http://zulko.github.io/blog/2013/09/27/read-and-write-video-frames-in-python-using-ffmpeg/
# - https://msdn.microsoft.com/en-us/library/windows/desktop/aa473780.aspx
# - https://superuser.com/a/1136854/101823
# - https://ffmpeg.org/ffmpeg-filters.html
# - https://ffmpeg.org/ffmpeg-filters.html#scale
# - https://ffmpeg.org/ffmpeg-filters.html#concat
import logging
import time
import ctypes
from enum import Enum
import tempfile
import subprocess
import os
import sys
import shutil
import libvirt
import termios
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from gi.repository import GLib
from gi.repository import GObject

import gi
gi.require_version('SpiceClientGLib', '2.0')
from gi.repository import SpiceClientGLib


# Output H.264 pixel format
# yuv444p is the default, but yuv420p is recommended for outdated media players.
# However, yuv420p requires H and W divisible by 2.
# We use yuv444p as the intermediate format when necessary, and re-encode to yuv420p with padding.
# https://github.com/mirror/x264/blob/90a61ec764/encoder/encoder.c#L501
H264_PIX_FMT_INTERMEDIATE = lambda disp: (
    'yuv420p' if disp.height % 2 == 0 and disp.width % 2 == 0 else 'yuv444p')
H264_PIX_FMT_FINAL = 'yuv420p'



quiet = False
def qprint(*args, **kwargs):
    if quiet:
        return
    return print(*args, **kwargs)


class SpiceSurfaceFmt(Enum):
    # spice-1/spice/enums.h
    SPICE_SURFACE_FMT_INVALID = 0
    SPICE_SURFACE_FMT_1_A = 1
    SPICE_SURFACE_FMT_8_A = 8
    SPICE_SURFACE_FMT_16_555 = 16
    SPICE_SURFACE_FMT_32_xRGB = 32
    SPICE_SURFACE_FMT_16_565 = 80
    SPICE_SURFACE_FMT_32_ARGB = 96


class Display(GObject.GObject):
    def __init__(self, index, channel, width, height, stride, shmid, imgdata, outfile):
        self.index   = index
        self.channel = channel
        self.width   = width
        self.height  = height
        self.stride  = stride
        self.shmid   = shmid

        # Ensure there is no width padding
        # stride [bytes] == width [pixels] * 4 [bytes / pixel]
        assert(self.stride == self.width * self.bytes_per_pixel)

        imgdata_bytes = stride * height
        self.imgdata = ctypes.cast(imgdata, ctypes.POINTER(ctypes.c_ubyte * imgdata_bytes))

        self.outfile = outfile(self) if callable(outfile) else outfile

        self._start_time = time.time()
        self._end_time = None
        self._num_frames_recorded = 0


    @staticmethod
    def get_format_class(format):
        return {
            SpiceSurfaceFmt.SPICE_SURFACE_FMT_32_xRGB:  Display32RGB,
            # TODO: Other formats
        }[format]

    def __repr__(self):
        return '{}(channel={}, width={}, height={}, stride={}, shmid={}, ' \
               'imgdata={}, outfile={})'.format(
                type(self), self.channel, self.width, self.height, self.stride,
                self.shmid, self.imgdata, self.outfile)


    def destroy(self):
        # Doesn't actually destroy anything -- called on destroy callback
        self.imgdata = None
        self._end_time = time.time()

    def write_frame(self):
        assert self.imgdata
        b = self._do_write_frame()
        self._num_frames_recorded += 1
        return b

    @property
    def frames_recorded(self):
        return self._num_frames_recorded

    @property
    def duration(self):
        end = self._end_time or time.time()
        return end - self._start_time


class Display32RGB(Display):
    bytes_per_pixel = 4
    ffmpeg_pix_fmt = 'bgr0'     # Each pixel is 4 bytes: BGR0,BGR0,...

    def _do_write_frame(self):
        return self.outfile.write(self.imgdata.contents)


class SpiceRecorder(GObject.GObject):
    __gsignals__ = {
        "periodic-update":      (GObject.SignalFlags.RUN_FIRST, None, []),
        "recording-stopped":    (GObject.SignalFlags.RUN_FIRST, None, [str]),
    }

    def __init__(self, domain, framerate=24, create_display_stream=None):
        GObject.GObject.__init__(self)

        self._vm = domain
        self._mainloop = GLib.MainLoop()

        self.framerate = framerate

        self._spice_session = None
        self._main_channel = None
        self._display_channel = None
        self._displays = []             # displays created, in chrono. order
        self._active_display = None     # currently active display

        self._last_periodic_update_t = None
        self._num_frames_recorded = 0
        self._start_time = None
        self._record_timeout_id = None

        self._create_display_stream = create_display_stream or self._create_display_tmpfile

    def _get_fd_for_open(self):
        # Reference:
        # https://github.com/virt-manager/virt-manager/blob/b8dccf6a/virtManager/viewers.py#L139

        uri = urlparse(self._vm._conn.getURI())
        if uri.hostname:
            # OpenGraphics only works for local libvirtd connections
            return None

        # TODO: Additional checks -- see virt-manager Viewer._get_fd_for_open

        fd = self._vm.openGraphicsFD(0,
                libvirt.VIR_DOMAIN_OPEN_GRAPHICS_SKIPAUTH)
        logging.debug('openGraphicsFD(0) returned %d', fd)
        return fd

    def _create_spice_session(self, conf={}):
        assert not self._spice_session
        self._spice_session = SpiceClientGLib.Session(read_only=True, **conf)
        SpiceClientGLib.set_session_option(self._spice_session)

        GObject.GObject.connect(self._spice_session, "channel-new",
                                self._channel_new_cb)

    def _channel_new_cb(self, session, channel):
        logging.debug("New channel signal: channel=%s", channel)

        GObject.GObject.connect(channel, "open-fd",
                                self._channel_open_fd_request)

        # Dispatch
        cb = {
            SpiceClientGLib.MainChannel:    self._new_main_channel,
            SpiceClientGLib.DisplayChannel: self._new_display_channel
        }.get(type(channel))
        if cb:
            cb(channel)

    def _channel_open_fd_request(self, channel, tls_ignore):
        logging.debug("Requesting fd for channel: %s", channel)

        fd = self._get_fd_for_open()
        channel.open_fd(fd)


    def _new_main_channel(self, channel):
            self._main_channel = channel
            self._main_channel.connect_after("channel-event",
                                        self._main_channel_event_cb)

    def _new_display_channel(self, channel):
            channel_id = channel.get_property("channel-id")
            if channel_id != 0:
                logging.warning("Spice multi-head unsupported")
                return

            if self._display_channel:
                logging.warning("Display channel already set")
                return
            self._display_channel = channel

            # See spice-gtk/src/spice-widget.c:channel_new()
            self._display_channel.connect_after("display-primary-create",
                                           self._display_primary_create_cb)
            self._display_channel.connect_after("display-primary-destroy",
                                           self._display_primary_destroy_cb)

            channel.connect()



    def _main_channel_event_cb(self, channel, event):
        logging.debug("Main channel %s event (%d) %s ", channel, event, event)

        if event == SpiceClientGLib.ChannelEvent.CLOSED:
            logging.debug('Main channel closed')
            self._stop_recording("Disconnected")

    def _create_display_tmpfile(self, display):
        return tempfile.NamedTemporaryFile('w+b', prefix='spice-record-',
                suffix='{}x{}.raw'.format(display.width, display.height), delete=False)

    def _display_primary_create_cb(self, channel, format, width, height, stride, shmid, imgdata):
        format = SpiceSurfaceFmt(format)
        logging.debug("display-primary-create channel=%s format=%s %dx%d", channel, format, width, height)
        if self._active_display:
            logging.warning("Hmm? _active_display is set: %s", self._active_display)
            return

        outf = self._create_display_stream
        d = Display.get_format_class(format)(len(self._displays), channel, width, height, stride, shmid, imgdata, outf)
        logging.debug("New display: %s", d)
        self._active_display = d
        self._displays.append(d)

        self._start_recording()


    def _display_primary_destroy_cb(self, channel):
        logging.debug("display-primary-destroy channel %s", channel)

        self._active_display.destroy()
        self._active_display = None

    def _start_recording(self):
        if self._record_timeout_id != None:
            return

        self._start_time = time.time()
        self._record_frame()
        self._record_timeout_id = GLib.timeout_add(
                int(1000 / self.framerate), self._record_frame)
        logging.debug('_record_timeout_id = %d', self._record_timeout_id)

        id_ = GLib.io_add_watch(
                sys.stdin,
                GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.IN,
                self._stdin_avail_cb,
                )

    def _stop_recording(self, reason=""):
        if self._record_timeout_id != None:
            logging.debug('Removing _record_timeout_id = %d', self._record_timeout_id)
            GLib.source_remove(self._record_timeout_id)
            self._record_timeout_id = None

        self.emit("recording-stopped", reason)
        self._mainloop.quit()


    def _record_frame(self):
        now = time.time()

        # Ugh, can we miss frames here while there is no display?
        if not self._active_display:
            return

        # Write the frame!
        b = self._active_display.write_frame()
        self._num_frames_recorded += 1

        # Perform periodic update
        if self._last_periodic_update_t:
            dt = now - self._last_periodic_update_t
        if not self._last_periodic_update_t or (dt > 1.0):
            self.emit("periodic-update")
            self._last_periodic_update_t = now

        return True

    def _stdin_avail_cb(self, cond, *data):
        for c in sys.stdin.read():
            c = c.upper()

            if c == 'Q':
                logging.info('Stopping on "Q" press')
                self._stop_recording("Requested by user")

        return True

    @property
    def displays(self):
        return list(self._displays)

    @property
    def elapsed_time(self):
        return time.time() - self._start_time

    @property
    def frames_recorded(self):
        return self._num_frames_recorded

    @property
    def bytes_recorded(self):
        return sum(getsize_or_zero(d.outfile.name) for d in self.displays)

    def get_resolution(self):
        if not self._display_channel:
            return None
        return self._display_channel.get_properties("width", "height")

    def _open_fd(self, fd):
        self._create_spice_session()

        logging.debug("Spice connecting via fd=%d", fd)
        self._spice_session.open_fd(fd)

    def _open_host(self):
        coninfo = domain_extract_connect_info(self._vm)
        logging.debug("Spice connecting to host=%s port=%s tlsport=%s",
            coninfo.ghost, coninfo.gport, coninfo.gtlsport)
        conf = {
            "host": coninfo.ghost,
            "port": coninfo.gport,
            "tls_port": coninfo.gtlsport,
            "password": coninfo.gpasswd,
        }

        self._create_spice_session(conf)
        self._spice_session.connect()

    def open(self):
        # References:
        #   - virt_viewer_open_connection()
        #   - virt-manager Viewer._open

        fd = self._get_fd_for_open()
        if fd is not None:
            self._open_fd(fd)
        else:
            self._open_host()

    def run(self):
        self._mainloop.run()

    def stop(self):
        logging.debug("Calling _mainloop.quit()")
        self._mainloop.quit()


def getsize_or_zero(path):
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def convert_concat_videos(displays, framerate, outcodec, outpath, loglevel=None):
    ffmpeg_args = [
        'ffmpeg',
        '-loglevel', loglevel,
        '-y',
    ]

    # Determine the output video resolution
    maxw, maxh = 0, 0
    for d in displays:
        maxw = max(maxw, d.width)
        maxh = max(maxh, d.height)

    # The FFmpeg 'pad' filter can't handle odd sizes, ensure they're even
    # TODO: Can we get rid of this now that we're using yuv444p?
    def align(x, align):
        return (x + align - 1) & ~(align - 1)
    maxw = align(maxw, 2)
    maxh = align(maxh, 2)

    # Establish temporary mp4 input files
    for d in displays:
        ffmpeg_args += [
            '-i', d.outfile.name,
        ]

    # Build our complex filtergraph:
    # 1) Create appropriately scaled versions of each input stream named [v{i}]
    filters = []
    for i,d in enumerate(displays):
        # Explained:
        #   [{i}:v]     Take the i'th input file's video stream,
        #   scale=      Scale it to w x h, maintaining aspect ratio,
        #               decreasing the output size if required to do so,
        #   pad=        And if so, pad it out to w x h, centering it in the frame.
        #   [v{i}]      Name the output stream [v{i}]
        filt = '[{i}:v] scale={w}:{h}:force_original_aspect_ratio=decrease,' \
                'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2  [v{i}]'.format(
                i=i, w=maxw, h=maxh)
        filters.append(filt)

    # 2) Concatenate the [v{i}] videos to [outv]
    # Explained:
    #   [v{i}]      Take each of the [v{i}] scaled streams,
    #   concat=     concatenate all 'n' of them together into one video output.
    #   [outv]      Name the output stream [outv]
    filt = ' '.join('[v{i}]'.format(i=i) for i in range(len(displays)))
    filt += 'concat=n={n}:v=1:a=0 [outv]'.format(n=len(displays))
    filters.append(filt)


    ffmpeg_args += [
        # Apply the compex filtergraph
        '-filter_complex', '; '.join(filters),

        # Specify output video parameters
        '-vcodec', outcodec,
        '-pix_fmt', H264_PIX_FMT_FINAL,

        # Map the [outv] to the output file
        '-map', '[outv]',
        outpath
    ]
    logging.debug("Invoking FFMPEG: {}".format(ffmpeg_args))
    subprocess.check_call(ffmpeg_args)

class FFmpegRawStream:
    """A stream of raw video to an FFMPEG process"""

    def __init__(self, path, display, framerate, outcodec, loglevel):
        self.path = path

        ffmpeg_args = [
            'ffmpeg',
            '-loglevel', loglevel,

            '-use_wallclock_as_timestamps', '1',

            # Input file
            '-f', 'rawvideo',
            '-vcodec', 'rawvideo',
            '-pix_fmt', display.ffmpeg_pix_fmt,
            '-r', str(framerate),
            '-an',
            '-s', '{}x{}'.format(display.width, display.height),
            '-i', 'pipe:0',     # stdin

            # Specify output video parameters
            '-vcodec', outcodec,
            '-pix_fmt', H264_PIX_FMT_INTERMEDIATE(display),

            # Ouptut file,
            self.path,
        ]
        logging.debug("Invoking FFMPEG: {}".format(ffmpeg_args))
        self.p = subprocess.Popen(ffmpeg_args, stdin=subprocess.PIPE)

    @property
    def name(self):
        # Mimics NamedTemporaryFile.name
        return self.path

    def write(self, data):
        return self.p.stdin.write(data)

    def close(self):
        self.p.stdin.close()
        rc = self.p.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, 'ffmpeg')


def domain_extract_connect_info(domain):
    # See
    #   virt_viewer_extract_connect_info()
    #   virt_viewer_app_set_connect_info()
    tree = ET.fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE))
    ET.dump(tree)

    gfx = tree.find('devices/graphics')

    class ConnectInfo:
        pass
    o = ConnectInfo()
    o.type = gfx.get('type')
    if o.type != 'spice':
        raise ValueError('Graphics type "{}" not supported; must be "spice"'.format(o.type))

    o.gport = gfx.get('port')
    o.gtlsport = gfx.get('tlsport')
    o.ghost = gfx.get('listen')
    o.unixsock = gfx.get('socket')
    o.gpasswd = gfx.get('passwd')

    if o.ghost and o.gport:
        logging.info('Guest graphics address is {}:{}'.format(o.ghost, o.gport))
    elif o.unixsock:
        logging.info('Guest graphics address is {}'.format(o.unixsock))
    else:
        logging.info('Using direct libvirt connection')

    uri = urlparse(domain._conn.getURI())
    o.host = uri.hostname
    o.transport = uri.scheme
    o.user = uri.username
    o.password = uri.password
    o.port = uri.port

    return o


def domain_wait(domain, target_state):
    printed = False
    while True:
        state, reason = domain.state()
        if state == target_state:
            break
        if not printed:
            qprint("Waiting for domain to enter state {}".format(target_state))
            printed = True
        time.sleep(0.1)         # TODO: Handle asynchronously: See virt_viewer_domain_event()

class TtyCbreakMode:
    LFLAG = 3
    CC = 6
    def __init__(self):
        self.orig_mode = None

    def __enter__(self):
        if sys.stdin.isatty():
            # Similar to tty.setcbreak()
            t = termios
            self.orig_mode = t.tcgetattr(sys.stdin.fileno())

            mode = t.tcgetattr(sys.stdin.fileno())
            mode[self.LFLAG] &= ~(t.ECHO | t.ICANON)
            mode[self.CC][t.VMIN] = 0
            mode[self.CC][t.VTIME] = 0
            t.tcsetattr(sys.stdin.fileno(), t.TCSADRAIN, mode)
        return self

    def __exit__(self, *excinfo):
        if self.orig_mode is not None:
            t = termios
            t.tcsetattr(sys.stdin.fileno(), t.TCSADRAIN, self.orig_mode)



def logging_to_ffmpeg_loglevel(ll):
    return {
        'DEBUG':    'debug',
        'INFO':     'info',
        'WARNING':  'warning',
        'ERROR':    'error',
        'CRITICAL': 'fatal',
    }[ll]



def format_datasize(b):
    suffixes = ['', 'ki', 'Mi', 'Gi', 'Ti']
    for s in suffixes:
        if (b < 1024) or (s == suffixes[-1]):
            break
        b /= 1024.0
    return '{:0.02f} {}B'.format(b, s)


def _record(args, dom, tmpdir):
    def create_ffmpeg_stream(display):
        path = os.path.join(tmpdir, '{:03}-{}x{}.mp4'.format(
            display.index, display.width, display.height))
        return FFmpegRawStream(
            path = path,
            display = display,
            framerate = args.framerate,
            outcodec = args.vcodec,
            loglevel = logging_to_ffmpeg_loglevel(args.loglevel),
            )

    domain_wait(dom, libvirt.VIR_DOMAIN_RUNNING)

    with TtyCbreakMode():
        # Open spice session
        sp = SpiceRecorder(dom,
                framerate = args.framerate,
                create_display_stream = create_ffmpeg_stream,
                )
        sp.open()

        # Record raw video
        def periodic_update(sp):
            qprint("\r" + " "*80, end="")
            qprint("\r{:<20}{:<20}{:<20}".format(
                "{:0.02f} sec".format(sp.elapsed_time),
                "{} frames".format(sp.frames_recorded),
                format_datasize(sp.bytes_recorded),
                ), end="")

        def on_stopped(sp, msg):
            qprint("\nRecording stopped:", msg)

        sp.connect("periodic-update", periodic_update)
        sp.connect("recording-stopped", on_stopped)

        qprint("Recording... Press Q to stop")
        sp.run()


    # Finalize all intermediate FFmpeg processes
    for d in sp.displays:
        rc = d.outfile.close()


    qprint("-"*80)
    qprint("Recorded displays:")
    maxw, maxh = 0, 0
    for n,d in enumerate(sp.displays):
        qprint("  {}: {}x{} {:>4} frames  {:>10}  {:0.02f} sec".format(n, d.width, d.height,
            d.frames_recorded,
            format_datasize(os.path.getsize(d.outfile.name)),
            d.duration))
        maxw = max(maxw, d.width)
        maxh = max(maxh, d.height)
    qprint("Final: {}x{}".format(maxw, maxh))
    qprint("-"*80)


    # Filter out displays with no frames
    displays = [d for d in sp.displays if d.frames_recorded]

    if len(displays) == 1 and H264_PIX_FMT_INTERMEDIATE(displays[0]) == H264_PIX_FMT_FINAL:
        # Optimization: use the only intermediate video as the final
        d = displays[0]
        src = d.outfile.name
        d.outfile = None
        logging.info("Moving {} to {}".format(src, args.output))
        shutil.move(src, args.output)

    else:
        # Convert video
        qprint("\nDone recording. Converting...")
        convert_concat_videos(
                displays = displays,
                framerate = args.framerate,
                outcodec = args.vcodec,
                outpath = args.output,
                loglevel = logging_to_ffmpeg_loglevel(args.loglevel),
                )
    qprint("\n{} written!".format(args.output))


def record(args, dom):
    tmpdir = tempfile.mkdtemp(prefix='spice-record-')
    try:
        _record(args, dom, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
