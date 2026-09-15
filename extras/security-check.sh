#!/usr/bin/env bash
# Static security audit check for Omantigravity (#6127)
# Fails CI if dangerous patterns, bare commands, or regressions are detected.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "=== OMANTIGRAVITY STATIC SECURITY AUDIT ==="

FAILED=0

# 1. Reject shell=True or bash -c
if grep -rnE "(shell\s*=\s*True|bash -c)" scripts/ Panel.qml; then
    echo "❌ ERROR: Detected forbidden shell execution (shell=True or bash -c)!"
    FAILED=1
else
    echo "✓ No shell=True or bash -c detected."
fi

# 2. Reject eval() in Python and JS/QML
if grep -rnE "\beval\s*\(" scripts/ Model.js Panel.qml; then
    echo "❌ ERROR: Detected forbidden eval() call!"
    FAILED=1
else
    echo "✓ No eval() calls detected."
fi

# 3. Reject os.path.expandvars
if grep -rnE "expandvars" scripts/; then
    echo "❌ ERROR: Detected forbidden expandvars usage!"
    FAILED=1
else
    echo "✓ No expandvars detected."
fi

# 4. Reject bare ping or curl commands in subprocess calls
if grep -rnE "subprocess\.(Popen|run|check_output|call)\(\s*\[\s*\"(ping|curl)\"" scripts/; then
    echo "❌ ERROR: Detected bare ping or curl invocation without absolute path or verified fd!"
    FAILED=1
else
    echo "✓ No bare ping or curl subprocess calls detected."
fi

# 5. Reject trace_id or response_id leakage into output dictionaries
if grep -rnE "\"trace_id\":\s*(resp|trace|m_trace)" scripts/; then
    echo "❌ ERROR: Detected raw trace_id or response_id assignment in scripts/!"
    FAILED=1
else
    echo "✓ No trace_id/response_id leakage detected in scripts."
fi

# 6. Validate plugin using omarchy CLI
if command -v omarchy >/dev/null 2>&1; then
    if ! omarchy plugin validate .; then
        echo "❌ ERROR: omarchy plugin validate failed!"
        FAILED=1
    else
        echo "✓ omarchy plugin validate passed."
    fi
fi

if [ "$FAILED" -ne 0 ]; then
    echo "=== STATIC SECURITY AUDIT FAILED ==="
    exit 1
fi

echo "=== STATIC SECURITY AUDIT PASSED ==="
exit 0
