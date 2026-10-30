#!/bin/bash
# segfault-es-replay.sh — one Event Service unit's simulation request run
# again, inside the campaign image on npps0 (swf-epicprod
# docs/SEGFAULT_DIAGNOSIS.md, Event Service units, Reproduction).
#
# Shipped and started by scripts/segfault-es-reproduce.py, never run on the
# monitor host. The working directory holds what the doer shipped:
#   payload/                the payload of the job's version
#   environment-replay.sh   the task's payload environment, registration and
#                           copies off, the resident simulation switched on
#   replay.env              REPLAY_FILE (EVGEN/<file>), REPLAY_EXT,
#                           REPLAY_EVENTS, REPLAY_CHUNK, REPLAY_SKIP,
#                           REPLAY_LABEL, REPLAY_SEED, REPLAY_TIMEOUT_S
#
# 1. run.sh runs the unit as a slot runs its first unit (es/es_slot.py,
#    run_unit): the unit's skip, chunk and label, the slot's resident
#    simulation socket, state and reply, and EPICPROD_UNIT_TEMPLATE, so
#    run.sh writes the request's npsim arguments beside the template. The
#    request the resident receives is the job's own: input, first event,
#    count and seed.
# 2. A resident death is DD4hep's "Handle signal: N" in the state
#    directory's resident.log. Then the same arguments run once more as a
#    cold npsim under gdb, over the request's events cut from the source
#    (es/evgen_ranges.py), with the request seed and no per-event seed,
#    which is how the resident seeds a request.
# 3. out/result.json, written last, is the outcome the doer collects:
#    crashed (with the signal), completed, or inconclusive (with the reason).
set -u
HERE=$(pwd)
source ./replay.env
mkdir -p run state out tmp gdb
# run.sh sources environment*.sh from its working directory.
cp environment-replay.sh run/
SOCK=/tmp/esreplay-$$.sock
export EPICPROD_SKIP_EVENTS="${REPLAY_SKIP}" EPICPROD_CHUNK_LABEL="${REPLAY_LABEL}"
export EPICPROD_SIM_SOCKET="${SOCK}" EPICPROD_SIM_STATE="${HERE}/state"
export EPICPROD_SIM_REPLY="${HERE}/out/simulation.json"
export EPICPROD_UNIT_TEMPLATE="${HERE}/out/unit-template.json"
export EPICPROD_CALL_HOME=0 EPICPROD_NTHREADS=1 REGISTRATION_STAGGER_MAX_S=0
# Scratch in the work tree: the container binds the host /tmp, which on
# npps0 is the small root volume (es_slot.py, TMPDIR).
export TMPDIR="${HERE}/tmp"
export PAYLOAD_STAGES_LOG="${HERE}/out/stages.log" PAYLOAD_REPORT="${HERE}/out/payload-report.json"
export PAYLOAD_JOB_REPORT="${HERE}/out/jobReport.json"

started=$(date +%s)
echo "replay: run.sh ${REPLAY_FILE} ${REPLAY_EXT} ${REPLAY_EVENTS} ${REPLAY_CHUNK}, skip ${REPLAY_SKIP}, label ${REPLAY_LABEL:-none}"
(cd run && timeout "${REPLAY_TIMEOUT_S}" "${HERE}/payload/run.sh" \
   "${REPLAY_FILE}" "${REPLAY_EXT}" "${REPLAY_EVENTS}" "${REPLAY_CHUNK}") > out/payload.log 2>&1
rc=$?
ended=$(date +%s)
echo "replay: run.sh exited ${rc} after $((ended - started)) s"

# A unit's exit leaves its resident up (resident_sim.py, request).
(cd payload/es && python3 -c 'import sys; from resident_sim import exchange; exchange(sys.argv[1], {"command": "stop"}, 10)' \
   "${SOCK}") > /dev/null 2>&1 || true
pkill -f -- "--socket ${SOCK}" 2>/dev/null || true

# The crash is the first crash-class signal DD4hep's handler reports (6, 7,
# 8, 11); a SIGTERM after it is the request timeout ending a resident that
# stalled after its signal, and a SIGTERM alone is no crash (41494, 2026-10-29:
# SIGABRT, a stall, then SIGTERM at 1,800 s).
signal=$(grep -oE 'Handle signal: (6|7|8|11)\b' state/resident.log 2>/dev/null | head -1 | grep -oE '[0-9]+$')
gdb_rc=""
if [ -n "${signal}" ] && [ -s out/unit-template.json.npsim.json ]; then
  # The request's npsim arguments as a cold run: the source's events cut to
  # a file of their own (skip 0), the request seed, no per-event seed.
  if python3 - out/unit-template.json.npsim.json gdb > gdb/args.log 2>&1 <<'EOF'
import json, os, sys
argv, d = json.load(open(sys.argv[1])), sys.argv[2]
out, source, i = [], '', 0
while i < len(argv):
    flag = argv[i]
    if flag == '--random.enableEventSeed':
        i += 1
        continue
    if flag in ('--skipNEvents', '--inputFiles', '--outputFile'):
        value = argv[i + 1]
        if flag == '--inputFiles':
            source = value
            value = os.path.abspath(os.path.join(d, 'range.hepmc3'))
        elif flag == '--skipNEvents':
            value = '0'
        else:
            value = os.path.abspath(os.path.join(d, 'out.edm4hep.root'))
        out += [flag, value]
        i += 2
        continue
    out.append(flag)
    i += 1
seed = argv[argv.index('--random.seed') + 1] if '--random.seed' in argv else ''
if seed != os.environ.get('REPLAY_SEED'):
    sys.exit(f'request seed {seed} is not the recorded {os.environ.get("REPLAY_SEED")}')
open(os.path.join(d, 'source'), 'w').write(source)
open(os.path.join(d, 'args'), 'w').write('\0'.join(out))
print('cold npsim arguments:', ' '.join(out))
EOF
  then
    python3 payload/es/evgen_ranges.py --input "$(cat gdb/source)" --first-event "${REPLAY_SKIP}" \
      --events "${REPLAY_EVENTS}" --output gdb/range.hepmc3 > gdb/cut.log 2>&1
    cut_rc=$?
    if [ "${cut_rc}" -eq 0 ]; then
      mapfile -d '' args < gdb/args
      npsim_py="$(dirname "$(readlink -f "$(command -v npsim)")")/npsim.py"
      (cd run && timeout "${REPLAY_TIMEOUT_S}" gdb -batch -ex run -ex bt -ex 'info registers rip' \
         --args python3 "${npsim_py}" "${args[@]}") > out/gdb.log 2>&1
      gdb_rc=$?
    else
      echo "replay: the range cut failed (exit ${cut_rc}); see gdb/cut.log"
    fi
  fi
fi

python3 - "${rc}" "${signal}" "${started}" "${ended}" "${gdb_rc}" > out/result.json.tmp <<'EOF'
import json, os, socket, sys
rc, signal, started, ended, gdb_rc = sys.argv[1:6]
def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ''
reply = {}
try:
    reply = json.loads(read('out/simulation.json') or '{}')
except ValueError:
    pass
stages = read('out/stages.log').strip().splitlines()
gdb = read('out/gdb.log')
stopped = next((l.split('Program received signal', 1)[1].strip().rstrip('.')
                for l in gdb.splitlines() if 'Program received signal' in l), '')
resident = read('state/resident.log')
stalled = signal and 'Handle signal: 15' in resident
if signal:
    outcome, reason = 'crashed', f'resident npsim died on signal {signal}' + (
        ', stalled after it and was killed at the request timeout' if stalled else '')
elif reply and not reply.get('error'):
    outcome, reason = 'completed', ''
else:
    outcome = 'inconclusive'
    reason = (f'run.sh exited {rc}' + (' (timed out)' if rc == '124' else '')
              + (' after the resident was killed at the request timeout with no crash signal'
                 if 'Handle signal: 15' in resident else ' with no simulation reply and no resident death')
              + (f'; last stage: {stages[-1]}' if stages else ''))
json.dump({'outcome': outcome, 'reason': reason, 'signal': int(signal) if signal else None,
           'payload_exit': int(rc), 'started_at': int(started), 'ended_at': int(ended),
           'host': socket.gethostname().split('.')[0],
           'simulation_reply': {k: reply.get(k) for k in ('events', 'seed', 'first_event',
                                                          'seed_mode', 'wall_s', 'error') if k in reply},
           'gdb': {'ran': bool(gdb), 'rc': int(gdb_rc) if gdb_rc else None, 'stopped_on': stopped},
           'stages': stages[-8:]}, sys.stdout, indent=1)
EOF
mv -f out/result.json.tmp out/result.json
echo "replay: result written"
