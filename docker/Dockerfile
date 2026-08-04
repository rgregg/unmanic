# Trawlarr container image.
#
# Three stages, so that the toolchain never reaches the shipped image:
#
#   btbn-ffmpeg  downloads and unpacks the BtbN static FFmpeg build
#   builder      builds /opt/venv (requirements + the Trawlarr wheel)
#   runtime      the image that actually ships
#
# Two things are worth knowing before editing this file.
#
# 1. Nothing here compiles FFmpeg. It used to, upstream; today ffmpeg comes
#    from the Jellyfin apt repository and from BtbN's prebuilt static
#    tarball. The compile toolchain and the `*-dev` headers that survived
#    that change were freight -- gcc, g++, cmake, meson, ninja, nasm, yasm,
#    autoconf, automake, libtool and every codec header -- and this file no
#    longer installs them anywhere but the builder stage, which is discarded.
#
# 2. The runtime stage installs a smaller package set than the builder, so
#    a library that resolved during the build can be absent at run time.
#    `docker/verify_runtime_libs.sh` runs inside the runtime stage and
#    fails the build if any ELF object the image ships has an unresolved
#    DT_NEEDED entry. Do not remove it: it is the only thing standing
#    between a trimmed apt list and an ffmpeg that dies on first transcode.
#
# Build from the project root, after building the wheel (see docker/README.md):
#   docker build -f ./docker/Dockerfile -t ghcr.io/rgregg/trawlarr:dev .

ARG BASE_IMAGE="ubuntu:24.04"


# ---------------------------------------------------------------------------
# Stage: btbn-ffmpeg
#
# Its own stage purely for build cache. The tarball is tens of MB over the
# network and is keyed only on BTBN_FFMPEG_RELEASE, so a change to
# requirements.txt or to the application wheel must not re-download it.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS btbn-ffmpeg

# Local fork: bumped from upstream's autobuild-2026-03-19-13-03 / version 8.0,
# which BtbN has since pruned (404 on the release). Their autobuild release
# tags get garbage-collected after a few weeks, so this needs occasional
# bumps. Newer 8.1 builds are stable; if/when BtbN moves to 8.2, update both
# values together (the URL filter inside the RUN below uses the version).
ARG BTBN_FFMPEG_RELEASE="autobuild-2026-07-27-14-00"
ARG BTBN_FFMPEG_VERSION="8.1"

RUN \
    echo "**** Configure APT ****" \
        && export DEBIAN_FRONTEND=noninteractive \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            ca-certificates \
            curl \
            tar \
            wget \
            xz-utils \
        && rm -rf /var/lib/apt/lists/*

# Always create the directory, even on an architecture BtbN does not
# publish for: the runtime stage copies it unconditionally, and a COPY
# --from of a path that does not exist fails the build.
RUN \
    echo "**** Download BtbN FFmpeg GPL build for $(uname -m) ****" \
        && mkdir -p /usr/lib/btbn-ffmpeg \
        && \
        if uname -m | grep -q x86; then \
            BTBN_ARCH="linux64"; \
        elif uname -m | grep -q aarch64; then \
            BTBN_ARCH="linuxarm64"; \
        else \
            BTBN_ARCH=""; \
        fi \
        && \
        if [ -n "${BTBN_ARCH}" ]; then \
            BTBN_FFMPEG_ASSET_URL="$(curl -fsSL "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/${BTBN_FFMPEG_RELEASE}" | grep -oE "https://[^[:space:]\\\"]*ffmpeg-n${BTBN_FFMPEG_VERSION}[^[:space:]\\\"]*-${BTBN_ARCH}-gpl-${BTBN_FFMPEG_VERSION}\\.tar\\.xz" | head -n1)" \
                && test -n "${BTBN_FFMPEG_ASSET_URL}" \
                && wget -qO /tmp/btbn-ffmpeg.tar.xz "${BTBN_FFMPEG_ASSET_URL}" \
                && tar -xJf /tmp/btbn-ffmpeg.tar.xz -C /usr/lib/btbn-ffmpeg --strip-components=1 \
                && rm -f /tmp/btbn-ffmpeg.tar.xz \
                && /usr/lib/btbn-ffmpeg/bin/ffmpeg -version | head -n1 ; \
        else \
            echo "**** No BtbN build published for $(uname -m); skipping ****" ; \
        fi


# ---------------------------------------------------------------------------
# Stage: builder
#
# Produces /opt/venv and nothing else. Everything installed here is
# discarded, so the apt list can be as generous as it needs to be: a
# compiler and python3-dev are present so that a requirement without a
# prebuilt wheel still builds from source.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ENV VIRTUAL_ENV=/opt/venv

RUN \
    echo "**** Configure APT ****" \
        && export DEBIAN_FRONTEND=noninteractive \
    && \
    echo "**** Install python build toolchain ****" \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            build-essential \
            ca-certificates \
            python3 \
            python3-dev \
            python3-pip \
            python3-setuptools \
            python3-venv \
        && rm -rf /var/lib/apt/lists/*

# The pip constraint is written here AND in the runtime stage. It is not
# copied between them on purpose: the runtime copy governs pip runs made by
# plugins and by /config startup scripts long after this build is over, and
# tying those to a file inherited from a discarded stage would make the two
# look coupled when they are not.
RUN \
    echo "**** Create virtual environment ****" \
        && python3 -m venv "${VIRTUAL_ENV}" \
    && \
    echo "**** Configure pip packages constraint and update python package tools ****" \
        && mkdir -p /etc/pip \
        && printf 'setuptools<81\n' > /etc/pip/setuptools-constraint.txt \
        && printf '[global]\nconstraint=/etc/pip/setuptools-constraint.txt\n' > /etc/pip.conf \
        && "${VIRTUAL_ENV}"/bin/pip install --no-cache-dir --upgrade \
            pip \
            setuptools \
            uv \
            wheel

# Requirements before the wheel: application code changes on every commit,
# the dependency set almost never does, and this ordering is what lets
# BuildKit reuse the expensive layer.
COPY /requirements.txt /tmp/requirements.txt
RUN \
    echo "**** Install pip packages into virtualenv ****" \
        && "${VIRTUAL_ENV}"/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Install the pre-built Trawlarr wheel.
# Must first build the package on the host, e.g.
#   python3 -m build --no-isolation --skip-dependency-check --wheel
COPY /dist/ /src/
RUN \
    echo "**** Install trawlarr from pre-built wheel ****" \
        && ls -l /src/ \
        && "${VIRTUAL_ENV}"/bin/pip install --no-cache-dir /src/trawlarr-*.whl

# A venv records the absolute path it was created at, and this one is
# copied to the same path in the runtime stage -- so nothing needs
# rewriting. Assert it anyway: if that ever stops being true, the symptom
# in the container is an import failure at boot, and this is a cheaper
# place to find out.
RUN \
    echo "**** Verify the virtualenv is importable ****" \
        && "${VIRTUAL_ENV}"/bin/python3 -c "import trawlarr; print('trawlarr from', trawlarr.__file__)" \
        && test -x "${VIRTUAL_ENV}"/bin/trawlarr


# ---------------------------------------------------------------------------
# Stage: runtime -- the shipped image
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime
LABEL maintainer="Josh.5 <jsunnex@gmail.com>"

# Env variables
ENV \
    LIBVA_DRIVERS_PATH="/usr/lib/x86_64-linux-gnu/dri" \
    NVIDIA_DRIVER_CAPABILITIES="compute,video,utility" \
    NVIDIA_VISIBLE_DEVICES="all"


# Install the runtime dependencies
RUN \
    echo "**** Configure APT ****" \
        && export DEBIAN_FRONTEND=noninteractive \
    && \
    echo "**** Install runtime packages ****" \
        && apt-get update \
        && apt-get install -y \
            ca-certificates \
            curl \
            gnupg \
            gosu \
            gzip \
            libexpat1 \
            libglib2.0-0 \
            libgomp1 \
            libharfbuzz0b \
            libmediainfo0v5 \
            libv4l-0 \
            libx11-6 \
            libxcb1 \
            libxext6 \
            libxml2 \
            tar \
            tzdata \
            wget \
            xz-utils \
    && \
    echo "**** Install all available Video Acceleration API packages ****" \
        # va-driver-all:
        #   A meta-package for installing all available Video Acceleration API (VAAPI) drivers. Includes packages like:
        #       - i965-va-driver
        #       - intel-media-va-driver
        #       - mesa-va-drivers
        #       - libdrm2
        #       - etc
        && apt-get install -y \
            va-driver-all \
    && \
    echo "**** Install all available Vulkan API packages ****" \
        && apt-get install -y \
            libvulkan1 \
            mesa-vulkan-drivers \
            vulkan-tools \
    && \
    echo "**** Install arch specific packages for $(uname -m) ****" \
        && sleep 2 \
        && \
        if uname -m | grep -q x86; then \
            echo "**** Add Intel Graphics repository  ****" \
                && wget -qO - https://repositories.intel.com/gpu/intel-graphics.key | gpg --dearmor --output /usr/share/keyrings/intel-graphics.gpg \
                && echo "deb [arch=amd64,i386 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu jammy client" | tee /etc/apt/sources.list.d/intel-gpu-jammy.list \
            && \
            echo "**** Install Intel Media Drivers  ****" \
                && apt-get update \
                # intel-media-va-driver-non-free:
                #   This is the primary driver for Intel's Video Acceleration API on Linux, which provides support for video encoding and decoding on Intel graphics hardware.
                # libigdgmm12:
                #   A part of the Intel Graphics Memory Management Library, which is used for managing graphics memory in an efficient and optimized way on Intel GPUs.
                # libmfx1:
                #   This is a library for Intel Media SDK runtime. It is necessary for applications that use Intel Quick Sync Video technology.
                # libmfxgen1:
                #   Similar to libmfx1, it's a part of the Intel Media SDK.
                # libva-drm2 and libva2:
                #   These are additional libraries needed for VAAPI to interface with the Direct Rendering Manager (DRM) and to provide the main API for VAAPI.
                # libvpl2:
                #   This is the successor to the Media SDK and provides a library for video processing.
                #   libvpl-dev used to be installed beside it, for building FFmpeg inside the
                #   container. Nothing builds FFmpeg here any more, so the headers are gone.
                && apt-get install -y \
                    intel-media-va-driver-non-free \
                    libigdgmm12 \
                    libmfx1 \
                    libmfxgen1 \
                    libva-drm2 \
                    libva2 \
                    libvpl2 \
            && \
            echo ; \
        fi \
    && \
    echo "**** Install hardware info tools packages ****" \
        # hwinfo:
        #   A hardware identification system that provides detailed information about all the hardware components of a computer.
        # vainfo:
        #   This utility displays information about the VAAPI capabilities of your hardware.
        && apt-get install -y \
            hwinfo \
            vainfo \
    && \
    echo "**** Section cleanup ****" \
        && apt-get clean autoclean -y \
        && apt-get autoremove -y \
        && rm -rf \
            /var/lib/apt/lists/* \
            /var/tmp/* \
            /tmp/*


# Install commonly used command line tools
ARG JELLYFIN_FFMPEG_VERSION="7"
ARG NODE_MAJOR="22"
RUN \
    echo "**** Configure APT ****" \
        && export DEBIAN_FRONTEND=noninteractive \
        && . /etc/os-release \
        && JELLYFIN_CODENAME="${VERSION_CODENAME:-noble}" \
    && \
    echo "**** Install FFmpeg for $(uname -m) ****" \
        && sleep 2 \
        && apt-get update \
        && \
        if uname -m | grep -qE 'x86|aarch64'; then \
            echo "**** Add Jellyfin repository ****" \
                && wget -qO - https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor --output /usr/share/keyrings/jellyfin_team.gpg \
                && echo "deb [arch=$( dpkg --print-architecture ) signed-by=/usr/share/keyrings/jellyfin_team.gpg] https://repo.jellyfin.org/ubuntu ${JELLYFIN_CODENAME} main" | tee /etc/apt/sources.list.d/jellyfin.list \
            && \
            echo "**** Install jellyfin-ffmpeg and linked 3rd party libs ****" \
                && apt-get update \
                && apt-get install --no-install-recommends --no-install-suggests -y \
                    locales \
                    openssl \
                && apt-get install -y \
                    jellyfin-ffmpeg${JELLYFIN_FFMPEG_VERSION} \
                && ln -s /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
                && ln -s /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe \
            && \
            echo ; \
        fi \
        && \
        if uname -m | grep -q aarch64; then \
            echo "**** Install arm64 FFmpeg support libraries ****" \
                && apt-get install --no-install-recommends --no-install-suggests -y \
                    libfontconfig1 \
                    libfreetype6 \
                    libomxil-bellagio-bin \
                    libomxil-bellagio0 \
            && \
            echo ; \
        fi \
    && \
    echo "**** Install startup script requirements ****" \
        # grc/grcat back /usr/bin/unmanic-tail-logs; sqlite3 backs the
        # entrypoint's SQLite maintenance; git/jq/nano/unzip are what
        # /config/startup.sh and plugin init.d scripts reach for.
        && apt-get install -y \
            git \
            grc \
            jq \
            nano \
            sqlite3 \
            unzip \
    && \
    echo "**** Add NodeJS repository ****" \
        # Node is a RUNTIME dependency, not a build one: installing a plugin
        # that ships a package.json runs `npm install` and `npm run build`
        # inside the container (see trawlarr/libs/plugins.py). It is the
        # largest single package in the image and it still has to be here.
        && wget -qO - https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor --output /usr/share/keyrings/nodesource.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && \
    echo "**** Install NodeJS for $(uname -m) ****" \
        && apt-get update \
        && apt-get install -y \
            nodejs \
    && \
    echo "**** Install exiftool for $(uname -m) ****" \
        && apt-get install -y \
            libimage-exiftool-perl \
    && \
    echo "**** Section cleanup ****" \
        && apt-get clean autoclean -y \
        && apt-get autoremove -y \
        && rm -rf \
            /var/lib/apt/lists/* \
            /var/tmp/* \
            /tmp/*


# Python interpreter for the copied virtualenv.
#
# No python3-dev and no compiler: this image runs Python, it does not build
# it. The consequence is real and deliberate. A plugin that opts in to
# dependency installation (see trawlarr/libs/plugin_dependencies.py) and
# declares a package with no wheel for this interpreter will now fail its
# install with a compiler error instead of building from source. That
# failure is loud and it aborts the plugin install rather than half-doing
# it; the escape hatch is a /config/startup.sh that apt-installs
# build-essential.
#
# python3-venv stays because docker/root/entrypoint.sh recreates /opt/venv
# with `python3 -m venv` if it ever finds it missing.
RUN \
    echo "**** Configure APT ****" \
        && export DEBIAN_FRONTEND=noninteractive \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            python3 \
            python3-venv \
    && \
    echo "**** Configure pip packages constraint ****" \
        && mkdir -p /etc/pip \
        && printf 'setuptools<81\n' > /etc/pip/setuptools-constraint.txt \
        && printf '[global]\nconstraint=/etc/pip/setuptools-constraint.txt\n' > /etc/pip.conf \
    && \
    echo "**** Section cleanup ****" \
        && apt-get clean autoclean -y \
        && apt-get autoremove -y \
        && rm -rf \
            /var/lib/apt/lists/* \
            /var/tmp/* \
            /tmp/*


# Bring in the artefacts the discarded stages produced.
ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
COPY --from=btbn-ffmpeg /usr/lib/btbn-ffmpeg /usr/lib/btbn-ffmpeg


# Fail the build here rather than at an operator's first transcode if the
# trimmed runtime package set has left a shipped ELF object without one of
# its libraries. See the script header for what it does and does not cover.
COPY /docker/verify_runtime_libs.sh /tmp/verify_runtime_libs.sh
RUN \
    echo "**** Verify runtime shared libraries resolve ****" \
        && bash /tmp/verify_runtime_libs.sh \
        && rm -f /tmp/verify_runtime_libs.sh \
    && \
    echo "**** Verify the application is present and runnable ****" \
        && "${VIRTUAL_ENV}"/bin/python3 -c "import trawlarr; print('trawlarr from', trawlarr.__file__)" \
        && ls "${VIRTUAL_ENV}"/lib/python3*/site-packages/trawlarr/webserver/public/index.html \
        && ffmpeg -version | head -n1 \
        && ffprobe -version | head -n1 \
        && /usr/lib/btbn-ffmpeg/bin/ffmpeg -version | head -n1


# Prepare default home directory for runtime user
RUN \
    echo "**** Prepare /config home directory ****" \
        && mkdir -p \
            /app \
            /config \
            /library \
            /tmp/unmanic \
        && chown 1000:1000 \
            /app \
            /config \
            /tmp/unmanic
ENV HOME_DIR="/config"


# Add local files
COPY /docker/root /

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/usr/bin/trawlarr"]

# Trawlarr runs on port 8888
EXPOSE 8888/tcp

# Health probe. The version endpoint is cheap and is registered before the
# scheduler/workers start, so a 200 here means the webserver is reachable.
# Long start-period covers the (slow) first-time DB migrations and plugin
# repo refresh that run on a clean container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS --max-time 4 http://127.0.0.1:8888/trawlarr/api/v2/version/read || exit 1
