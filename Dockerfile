# Vault-native: this image contains no Obsidian and never talks to one. The
# server reads and writes the markdown files directly, which is the only thing
# that works on a headless server. (Obsidian's official CLI, and the Quartz Syncer
# plugin's CLI, both require a *running* Obsidian app -- see README.)
# Pinned by digest; Renovate proposes digest updates weekly (renovate.json).
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# No Node. Optional Quartz publishing assumes the site builds on its host
# (Vercel, Netlify, GitHub Pages...) when pushed, so building it here would only
# duplicate that -- at the cost of ~400 MB of Node in the image and a full
# `npm install` of the site's dependencies. publish_site stages content, commits
# and pushes; set QUARTZ_BUILD_LOCALLY=false to skip the local build step.
#
# The trade-off is deliberate: a local build is a gate that stops a broken note
# from shipping. The hosting provider's build catches that instead, after the
# push rather than before it, and reports it on the deployment.

# Pinned by digest: a moving tag would silently change the builder.
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /bin/uv

# ripgrep backs search. git and openssh-client back Quartz publishing
# (staging content, committing, and pushing to the site repo).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ripgrep git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OBSIDIAN_VAULT_ROOT=/vaults \
    OBSIDIAN_MCP_STATE_DIR=/state \
    QUARTZ_REPO_PATH=/quartz \
    OBSIDIAN_MCP_PORT=8780

WORKDIR /app
# Dependencies come from uv.lock, so the image ships exactly the versions CI
# tested -- not whatever resolves on the day it is built. The project itself is
# installed afterwards with --no-deps for the same reason.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps . \
 && rm /tmp/requirements.txt

# A real passwd entry for uid 1000, not just the numeric id. ssh refuses to run
# as a uid it cannot resolve -- "No user exists for uid 1000" -- which broke
# git push from inside the container while working fine on the host, where the
# uid does resolve. Everything else about the container was identical, so the
# failure only appeared on a real publish.
RUN useradd --uid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app

# All three are bind-mounted over at run time; creating them keeps the image
# runnable standalone for a smoke test.
RUN mkdir -p /vaults /state /quartz && chown -R 1000:1000 /vaults /state /quartz /app
ENV HOME=/home/app
USER 1000:1000

# Stamps the commit this image was built from, so a running container can say
# what it contains. CI passes the commit SHA; a local `docker build` without
# --build-arg leaves it "unknown", which honestly says "not built from a known
# commit". Declared here, late, so changing it does not invalidate the
# dependency layers above.
#
# The base image and the uv copy are both pinned by digest, and Python deps come
# from uv.lock, so a rebuild pulls the same layers and packages until a Renovate
# PR moves them in git. apt-get packages can still differ between builds.
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.revision="$GIT_REVISION"
LABEL org.opencontainers.image.source="https://github.com/JasonSooter/obsidian-mcp"
LABEL org.opencontainers.image.title="obsidian-mcp"

EXPOSE 8780

# Hits the one unauthenticated route. It returns 503 when a vault root is not
# readable -- which is what actually breaks in practice, since the vault usually
# arrives from a sync tool (Dropbox, rclone, Syncthing) that can be down or
# mid-resync.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8780/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "obsidian_mcp"]
