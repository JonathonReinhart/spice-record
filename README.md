[spice-record]
============
This is a simple utility for recording a [SPICE] sesion to MP4 video.
It uses libvirt to connect to the VMs, `SpiceClientGLib` to access the graphics
device, and FFmpeg to encode MP4 videos.

## Usage
```
usage: spice-record [-h] [--vcodec VCODEC]
                    [--loglevel {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
                    [-r FRAMERATE] [-c LIBVIRT_URI] [-o FILENAME]
                    DOMAIN-NAME|ID|UUID

positional arguments:
  DOMAIN-NAME|ID|UUID   Machine to record

optional arguments:
  -h, --help            show this help message and exit
  --vcodec VCODEC       Set the output video codec (see "ffmpeg -encoders" for
                        choices)
  --loglevel {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set the logging level (default=WARNING)
  -r FRAMERATE, --framerate FRAMERATE
  -c LIBVIRT_URI, --connect LIBVIRT_URI
                        Connect to hypervisor (e.g. qemu:///system)
  -o FILENAME, --output FILENAME
                        Output filename (defaults to <domain-name>.mp4)
```

## Requirements
- Python 3
- `libvirt-python` (not `libvirt-glib`)
- `spice-glib`
- `pygobject3`
- `ffmpeg`

If `virt-manager` is installed on a modern distro (which has ported all of its
Python apps to Python 3), then everything should already be installed, aside
from `ffmpeg`.

[spice-record]: https://github.com/JonathonReinhart/spice-record
[SPICE]: https://www.spice-space.org/
