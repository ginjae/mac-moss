# Third-party notices

Mac MOSS is an improved derivative of
[jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note), which is a
fork of [JoNebula/moss-note](https://github.com/JoNebula/moss-note).
The application source is distributed under the [MIT License](LICENSE).

## Application provenance

| Project | Attribution | License evidence |
|---|---|---|
| Original MOSS Note | Jo Seongwoon (`JoNebula`), original application and contributors | [Original LICENSE](https://github.com/JoNebula/moss-note/blob/2fbb57e40d80009e6e6a3195085001ce133a1713/LICENSE) |
| Local MOSS Note fork | `jiseungjeong` and contributors, Apple Silicon / MLX adaptation used as the basis for Mac MOSS | [Fork LICENSE](https://github.com/jiseungjeong/moss-note/blob/258be7bd48fc2d75579f2d24a9f64d7543635285/LICENSE) |

Both referenced license files contain `Copyright (c) 2026 Jo Seongwoon` and
the same MIT license text. That file is preserved verbatim in this repository's
[LICENSE](LICENSE). The fork's contributions are credited here and in the
[README](README.md). Mac MOSS's modifications are described in the README.

The pinned links identify the license revisions reviewed for this notice; they
do not assert the exact commits from which all local code was derived.

## Models and inference runtime

Model weights and inference packages are downloaded during setup. They are not
vendored in this source repository. The default MLX model is a community
conversion by `vanch007` of the OpenMOSS model.

| Component | License | Upstream evidence |
|---|---|---|
| MOSS-Transcribe-Diarize 0.9B · OpenMOSS / MOSI.AI | Apache-2.0 | [Model card](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize), [source license](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/blob/main/LICENSE) |
| MLX 8-bit MOSS conversion · vanch007 | Apache-2.0 | [Default model card](https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize-8bit) |
| mlx-audio 0.5.7 · Prince Canuma and contributors | MIT | [License at v0.5.7](https://github.com/Blaizzy/mlx-audio/blob/v0.5.7/LICENSE) |
| mlx-lm 0.31.3 · Apple and contributors | MIT | [License at v0.31.3](https://github.com/ml-explore/mlx-lm/blob/v0.31.3/LICENSE) |
| MLX · Apple and contributors | MIT | [Source license](https://github.com/ml-explore/mlx/blob/main/LICENSE) |

The application MIT license does not replace these licenses. If downloaded
models or inference packages are redistributed with an application bundle,
retain their applicable license and attribution files and follow their
redistribution terms, including Apache-2.0 section 4 where applicable. A custom
`MLX_MODEL_ID` may select a model with different terms.

## Web application dependencies

The following direct runtime dependencies are installed from PyPI rather than
included in this source repository:

| Package | License | Source |
|---|---|---|
| FastAPI | MIT | [Package metadata](https://pypi.org/project/fastapi/) |
| HTTPX | BSD-3-Clause | [Package metadata](https://pypi.org/project/httpx/) |
| python-multipart | Apache-2.0 | [Package metadata](https://pypi.org/project/python-multipart/) |
| Uvicorn | BSD-3-Clause | [Package metadata](https://pypi.org/project/uvicorn/) |

Transitive dependencies and additional MLX runtime packages retain their own
licenses in the installed environments. These tables identify the main
components used by this project; they are not a complete inventory of a
redistributed binary bundle.

## FFmpeg

Mac MOSS invokes `ffmpeg` and `ffprobe` as separate executables installed by the
user through Homebrew. FFmpeg binaries are not included in this repository.
FFmpeg is LGPL-2.1-or-later by default; enabling GPL components changes the
applicable license, as described in [FFmpeg's licensing information](https://ffmpeg.org/legal.html).
If distributing an FFmpeg binary, follow the terms of that specific build.
