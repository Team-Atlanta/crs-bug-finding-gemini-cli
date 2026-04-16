# =============================================================================
# crs-bug-finding-gemini-cli Finder Module
# =============================================================================
# RUN phase: Analyzes source code and crafts POV inputs using Gemini CLI.
#
# Uses pre-built ASAN harness from the build phase — no builder sidecar needed.
# =============================================================================

# These ARGs are required by the oss-crs framework template
ARG target_base_image
ARG crs_version

FROM gemini-cli-bug-finding-base:cli-0.9.0

# Install libCRS (CLI + Python package)
COPY --from=libcrs . /libCRS
RUN pip3 install /libCRS \
    && python3 -c "from libCRS.base import DataType; print('libCRS OK')"

# Install crs-bug-finding-gemini-cli package (finder + agents)
COPY pyproject.toml /opt/crs-bug-finding-gemini-cli/pyproject.toml
COPY finder.py /opt/crs-bug-finding-gemini-cli/finder.py
COPY agents/ /opt/crs-bug-finding-gemini-cli/agents/
RUN pip3 install /opt/crs-bug-finding-gemini-cli

CMD ["run_finder"]
