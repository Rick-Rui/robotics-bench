#!/bin/bash

# Fail tasks whose code or configuration names a path on the author's own
# machine: an AutoDL data disk, a private conda environment under /workspace,
# a personal home directory, or a macOS/Windows user folder.
#
# Such a path exists only where the task was written. In a fresh container it
# either does not resolve, or the task works around it (a symlink at the
# author's path, a private base image), and then the task under review is not
# the task that runs. Use paths the task's own Dockerfile creates, or
# environment variables the platform provides.
#
# Scope: instruction.md, task.toml and code/config files under environment/,
# tests/ and solution/. authoring/ is skipped: it never enters a container, and
# provenance records may legitimately name the machine data came from. Data
# files (.cif, .csv, ...) are skipped because third-party data often embeds
# its origin path in metadata.
#
# Usage:
#   ./check-author-host-paths.sh                 # Check all tasks
#   ./check-author-host-paths.sh tasks/a/b/c     # Check specific tasks

# Container users that Dockerfiles create on purpose.
ALLOWED_HOME_USERS="agent user"

PATTERN='/root/autodl-tmp|/autodl-(fs|pub)(/|$)|/workspace/(miniconda|anaconda)[0-9]*|/Users/[A-Za-z0-9_.-]+/|[A-Za-z]:\\\\Users\\\\|/home/[A-Za-z0-9_.-]+/'

if [ $# -eq 0 ]; then
    TASK_DIRS=$(find tasks -name task.toml -exec dirname {} \; | sort)
else
    TASK_DIRS="$*"
fi

FAILED=0
for task_dir in $TASK_DIRS; do
    task_dir="${task_dir%/}"
    [ -d "$task_dir" ] || continue
    files=$( { ls "$task_dir/instruction.md" "$task_dir/task.toml" 2>/dev/null
              for sub in environment tests solution; do
                  [ -d "$task_dir/$sub" ] || continue
                  find "$task_dir/$sub" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.bash' \
                      -o -name '*.toml' -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' \
                      -o -name '*.cfg' -o -name '*.ini' -o -name '*.md' -o -name '*.kit' \
                      -o -name 'Dockerfile*' -o -name '*.dockerfile' \) -size -2M
              done; } | sort -u)
    [ -n "$files" ] || continue
    # grep -o prints "file:line:match"; task paths contain no colons.
    while IFS=: read -r file line match; do
        [ -n "$match" ] || continue
        case "$match" in
            /home/*)
                user=$(printf '%s' "$match" | cut -d/ -f3)
                case " $ALLOWED_HOME_USERS " in *" $user "*) continue ;; esac
                ;;
        esac
        echo "FAIL $file:$line: author-machine path \"$match\""
        FAILED=1
    done < <(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nIoE "$PATTERN" /dev/null 2>/dev/null)
done

if [ "$FAILED" -eq 1 ]; then
    echo ""
    echo "Replace author-machine paths with paths the task's Dockerfile creates or with"
    echo "platform-provided environment variables. authoring/ is not checked."
    exit 1
fi

echo "No author-machine paths found"
