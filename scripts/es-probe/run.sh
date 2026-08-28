#!/bin/bash
# Submit the Event Service probe task: a tiny ES-mode task
# (nEventsPerWorker in the taskParamMap) against the production queue,
# probing the JEDI ES generation path for the epic VO — task
# acceptance, jedi_events range creation, ES job lifecycle, and range
# disposition after a payload that does not consume ranges. The
# payload is a short sleep; its failure to speak the range channel is
# part of the probe. One small job's minutes of site time.
#
# Usage: bash run.sh
# Requires the cached panda-client OIDC token (~/pclient/run/setup.sh).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d%H%M)"
WORK="${SWF_TMP_DIR:-/data/swf-tmp}/es-probe/${STAMP}"
mkdir -p "${WORK}/sandbox"
echo "ES probe sandbox placeholder (payload uses no sandbox files)" \
    > "${WORK}/sandbox/README"
sed "s/%STAMP%/${STAMP}/" "${HERE}/spec.json" > "${WORK}/spec.json"
echo "spec: ${WORK}/spec.json"
grep outDS "${WORK}/spec.json"
source ~/pclient/run/setup.sh
export PANDA_AUTH_VO=EIC.production
python3 "${HERE}/../evgen_panda_submit.py" \
    --spec "${WORK}/spec.json" --workdir "${WORK}/sandbox"
