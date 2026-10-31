#!/usr/bin/env bash
# Installs what the fence's checks run. Written by the factory onboarding; edit it to match the repository.
set -euo pipefail
# Java 17 (maven): download dependencies and compile once, so the checks below run offline.
./mvnw -B -ntp -q -Dspring-javaformat.skip=true -Dcheckstyle.skip=true -DskipTests test-compile
