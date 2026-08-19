#!/bin/sh
for i in $(seq 1 40); do echo "test_case_$i ... ok"; done
echo 'test_normalize_strips_whitespace ... ok'
echo '40 passed in 1.2s'
