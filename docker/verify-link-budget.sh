#!/usr/bin/env bash

# Compare the last distance-derived link-budget record produced by both
# post-processing endpoints of one physical QKD link.
verify_qkd_link_budget_pair() {
    local docker_command="$1" left_container="$2" right_container="$3" link_name="$4"
    local left_line right_line

    left_line=$("$docker_command" logs "$left_container" 2>&1 |
        grep "\[QKD_LINK_BUDGET\] link=${link_name} " | tail -n 1 || true)
    right_line=$("$docker_command" logs "$right_container" 2>&1 |
        grep "\[QKD_LINK_BUDGET\] link=${link_name} " | tail -n 1 || true)

    if [ -z "$left_line" ] || [ -z "$right_line" ]; then
        echo "  [FAIL] missing QKD link-budget record for ${link_name}"
        return 1
    fi
    if [ "$left_line" != "$right_line" ]; then
        echo "  [FAIL] QKD link-budget mismatch for ${link_name}"
        echo "         left:  ${left_line}"
        echo "         right: ${right_line}"
        return 1
    fi

    echo "  [OK] ${left_line}"
}
