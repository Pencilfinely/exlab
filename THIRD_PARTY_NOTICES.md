# Third-party components

Experiment Manager source code is licensed under the MIT license in `LICENSE`.

The Windows controller ZIP redistributes the official CPython 3.13.15 embeddable
distribution from the Python Software Foundation. Its license text, historical
licenses and notices are preserved in `runtime/LICENSE.txt`. The application
changes only `runtime/python313._pth` to include its own application directory.
Source and archive checksum: [Python 3.13.15](https://www.python.org/downloads/release/python-31315/).

Workers download Docker images at setup time. PyTorch, CUDA, cuDNN, NumPy and
the OCI registry retain their upstream licenses; those images are not included
in these ZIPs. Docker, WSL, Ubuntu and NVIDIA drivers/toolkit are separately
installed prerequisites, not part of this project's MIT grant.

The SASRec adapter calls user-provided algorithm code. No external SASRec
implementation, private research repository, dataset or trained model is bundled
in the public source repository or the three release ZIPs. Users retain
responsibility for the licenses of the projects and datasets they choose to run.

The worker includes the Linux x64 py-spy 0.4.2 executable (MIT), exclusively for
bounded, nonblocking Python stack observations of an owned training container.
License: expman/vendor/py-spy-LICENSE.txt. Upstream: https://github.com/benfred/py-spy
Official PyPI wheel SHA256: aeb0323409199c785f730645e9f4bb7a7b9ca2c481f2c331a55642b5d13fa52f
Executable SHA256: 9b4d1f39b2a47ae44f4c6a46f615dcc0287d7755beba5065f32391951e07d594
No package download occurs on a worker while collecting an observation.
