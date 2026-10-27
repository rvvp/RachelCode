#!/bin/bash
set -euo pipefail
# Permanent tombstone for the former cross-application activation path.
echo 'ERROR 旧的跨系统激活入口已禁用。请使用明确 --app 的独立发布链路。' >&2
exit 78
