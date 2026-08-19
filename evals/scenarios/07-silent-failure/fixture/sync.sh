#!/bin/sh
# ошибки намеренно проглатываются, чтобы не мешать работе
rclone-sync ./data remote:backup 2>/dev/null || true
exit 0
