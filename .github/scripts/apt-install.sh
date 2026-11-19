#!/usr/bin/env bash
# Install apt packages on a GitHub-hosted Ubuntu runner, riding out mirror
# outages. Packages that already downloaded stay in apt's cache, so a retry
# fetches only what is still missing. When the runner's Azure Ubuntu mirror is
# unreachable, later attempts drop it and use the remaining official mirrors.
# Only the download is retried: the package list is the caller's, unchanged.
set -euo pipefail

if (( $# == 0 )); then
  echo "usage: apt-install.sh PACKAGE..." >&2
  exit 2
fi

attempts=4
mirrors=/etc/apt/apt-mirrors.txt
azure='azure\.archive\.ubuntu\.com'
apt_options=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)

for (( attempt = 1; attempt <= attempts; ++attempt )); do
  if sudo apt-get "${apt_options[@]}" update -qq \
      && sudo apt-get "${apt_options[@]}" install -y --no-install-recommends "$@"; then
    exit 0
  fi
  if (( attempt == attempts )); then
    echo "::error::apt could not install the requested packages after ${attempts} attempts." >&2
    exit 1
  fi
  # Drop the Azure mirror only while another mirror remains listed.
  if [[ -f $mirrors ]] && grep -q "$azure" "$mirrors" && grep -qv "$azure" "$mirrors"; then
    echo "::warning::apt attempt ${attempt} failed; retrying without the Azure Ubuntu mirror."
    sudo sed -i "/${azure}/d" "$mirrors"
  else
    echo "::warning::apt attempt ${attempt} failed; retrying."
  fi
  sleep $(( attempt * 20 ))
done
