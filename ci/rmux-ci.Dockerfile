FROM public.ecr.aws/docker/library/node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b AS node-runtime
FROM public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS python-runtime
FROM public.ecr.aws/docker/library/rust:1.95.0-bookworm@sha256:6258907abe69656e41cd992e0b705cdcfabcbbe3db374f92ed2d47121282d4a1 AS toolchain

ARG TARGETARCH
ARG BUN_VERSION=1.3.14
ARG BUN_X64_SHA256=951ee2aee855f08595aeec6225226a298d3fea83a3dcd6465c09cbccdf7e848f
ARG BUN_ARM64_SHA256=a27ffb63a8310375836e0d6f668ae17fa8d8d18b88c37c821c65331973a19a3b
ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_TARGET_DIR=/opt/rmux-target \
    CARGO_INCREMENTAL=0 \
    CARGO_PROFILE_DEV_DEBUG=0 \
    CARGO_PROFILE_TEST_DEBUG=0 \
    CMUX_GHOSTTY_SRC=/opt/ghostty \
    NPM_CONFIG_CACHE=/opt/npm-cache \
    ZIG_GLOBAL_CACHE_DIR=/opt/zig-global-cache \
    PATH=/opt/bun:/usr/local/bin:${PATH}

COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=python-runtime /usr/local/ /usr/local/
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates clang curl git libclang-dev pkg-config unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/install-zig-ci.sh scripts/ghostty-zig-version.sh /opt/rmux-scripts/
COPY ghostty/build.zig.zon /opt/ghostty/build.zig.zon
RUN ZIG_FORCE_LOCAL_INSTALL=1 RUNNER_TEMP=/opt/zig-cache /opt/rmux-scripts/install-zig-ci.sh \
    && ZIG="$(find /opt/zig-cache -type f -name zig -perm -u+x -print -quit)" \
    && test -n "$ZIG" \
    && ln -s "$ZIG" /usr/local/bin/zig \
    && zig version
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) asset=bun-linux-x64; checksum="$BUN_X64_SHA256" ;; \
      arm64) asset=bun-linux-aarch64; checksum="$BUN_ARM64_SHA256" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/${asset}.zip"; \
    printf '%s  %s\n' "$checksum" /tmp/bun.zip | sha256sum --check -; \
    unzip -q /tmp/bun.zip -d /tmp/bun; \
    mkdir -p /opt/bun; \
    install -m 0755 "/tmp/bun/${asset}/bun" /opt/bun/bun; \
    rm -rf /tmp/bun /tmp/bun.zip
RUN cargo install cargo-chef --version 0.1.73 --locked \
    && python3 -m pip install --disable-pip-version-check --no-cache-dir pytest==8.4.1

FROM toolchain AS rust-planner
WORKDIR /workspace/cmux-tui
COPY cmux-tui/ ./
RUN cargo chef prepare --recipe-path /tmp/recipe.json

FROM toolchain AS rust-deps
WORKDIR /workspace/cmux-tui
COPY --from=rust-planner /tmp/recipe.json /tmp/recipe.json
COPY cmux-tui/vendor/ ./vendor/
COPY ghostty/ /opt/ghostty/
RUN mkdir -p /opt/zig-global-cache/tmp && cd /opt/ghostty && zig build --fetch=all
RUN cargo chef cook --workspace --locked --recipe-path /tmp/recipe.json

FROM rust-deps AS rust-cache
WORKDIR /opt/rmux-source/cmux-tui
COPY cmux-tui/ ./
ARG RMUX_BUILD_COMMIT
RUN test -n "$RMUX_BUILD_COMMIT" \
    && CMUX_TUI_BUILD_COMMIT="$RMUX_BUILD_COMMIT" cargo test --workspace --locked --no-run \
    && printf '%s\n' "$RMUX_BUILD_COMMIT" > /opt/rmux-source-tree
RUN npm --prefix bindings/typescript ci --no-audit --no-fund --cache /opt/npm-cache

FROM toolchain AS ci-runtime
COPY --chown=65532:65532 --from=rust-cache /opt/rmux-target/ /opt/rmux-target/
COPY --chown=65532:65532 --from=rust-cache /opt/npm-cache/ /opt/npm-cache/
COPY --chown=65532:65532 --from=rust-cache /usr/local/cargo/registry/ /usr/local/cargo/registry/
COPY --chown=65532:65532 --from=rust-cache /opt/zig-global-cache/ /opt/zig-global-cache/
COPY --chown=65532:65532 --from=rust-cache /opt/rmux-source/ /opt/rmux-source/
COPY --chown=65532:65532 --from=rust-cache /opt/rmux-source-tree /opt/rmux-source-tree
COPY --chown=65532:65532 ghostty/ /opt/ghostty/
RUN find /opt/rmux-source -type d -exec chmod 0755 {} +
WORKDIR /workspace
