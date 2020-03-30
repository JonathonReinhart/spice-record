import os
from setuptools import setup
import spicerecord.version


def read_project_file(path):
    proj_dir = os.path.dirname(__file__)
    path = os.path.join(proj_dir, path)
    with open(path, 'r') as f:
        return f.read()


setup(
    name = 'spicerecord',
    version = spicerecord.version.__version__,
    python_requires='>=3.5',
    description = 'Record SPICE session to MP4 video',
    long_description = read_project_file('README.md'),
    long_description_content_type = 'text/markdown',
    classifiers = [
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Operating System :: POSIX :: Linux',
    ],
    license = 'GPL v2 with wrapper exception',
    author = 'Jonathon Reinhart',
    author_email = 'jonathon.reinhart@gmail.com',
    url = 'https://github.com/JonathonReinhart/spice-record',
    packages = ['spicerecord'],
    entry_points = {
        'console_scripts': [
            'spice-record = spicerecord.cli:main',
        ]
    },
    install_requires = [
        'libvirt-python',
        'PyGObject',
        # 'SpiceClientGLib' -- Not sure how to express this here.
    ],
)
