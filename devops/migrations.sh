#!/usr/bin/env bash

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )";

if [[ ! -x $(command -v pw_migrate) ]]; then
    echo "Missing dependency 'pw_migrate'.";
    echo "Ensure requirements.txt is satisfied first.";
    echo
    echo "Run:";
    echo "python3 -m pip install --user --upgrade -r $(realpath ${SCRIPT_DIR}/../requirements.txt)";
    echo
    exit 1;
fi

# NOTE: 'realpath -m' - the database may not exist yet, and plain realpath
# fails on a missing path, which silently left the --database argument empty.
DATABASE_FILE=$(realpath -m "${HOME}/.trawlarr/config/trawlarr.db");
TEST_DATABASE_FILE=$(realpath -m "${SCRIPT_DIR}/../tests/tmp/config/.trawlarr/config/trawlarr.db");
if [[ -f ${TEST_DATABASE_FILE} ]]; then
    DATABASE_FILE=${TEST_DATABASE_FILE}
fi

# These two must match trawlarr/service.py:init_db(). The application loads its
# migrations from 'trawlarr/migrations_v1' and records what it has applied in
# 'migratehistory_v1' (see MIGRATIONS_DIR / MIGRATIONS_HISTORY_VERSION and
# Migrations.__init__). This script previously pointed at 'trawlarr/migrations',
# which does not exist, and let pw_migrate default to the 'migratehistory'
# table - so anything created here was invisible to the application, and
# 'list'/'migrate' reported against a history table nothing else writes.
MIGRATIONS_PATH=$(realpath -m "${SCRIPT_DIR}/../trawlarr/migrations_v1");
MIGRATIONS_TABLE="migratehistory_v1";


# Parse args
ARGS="--database=sqlite:///${DATABASE_FILE} --directory=${MIGRATIONS_PATH} --migratetable=${MIGRATIONS_TABLE}"
COMMAND=""
for ARG in ${@}; do
    if [[ "${ARG}" == "--help" || "${ARG}" == "-h" ]]; then
        pw_migrate --help;
        echo
        exit 0;
    elif [[ "${ARG}" == "create" ]]; then
        COMMAND="create";
        continue;
    elif [[ "${ARG}" == "list" ]]; then
        COMMAND="list";
        continue;
    elif [[ "${ARG}" == "merge" ]]; then
        COMMAND="merge";
        continue;
    elif [[ "${ARG}" == "migrate" || "${ARG}" == "run" ]]; then
        COMMAND="migrate";
        continue;
    elif [[ "${ARG}" == "rollback" ]]; then
        COMMAND="rollback";
        continue;
    fi
    ARGS="${ARGS} ${ARG}";
done

echo "pw_migrate ${COMMAND} ${ARGS}";
pw_migrate ${COMMAND} ${ARGS};
