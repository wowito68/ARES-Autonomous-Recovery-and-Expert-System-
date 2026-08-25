#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

model_name=${ARES_AI_MODEL:-qwen2.5:1.5b-instruct-q4_K_M}
expected_model=qwen2.5:1.5b-instruct-q4_K_M
ollama_version=${ARES_OLLAMA_VERSION:-v0.32.15}
ollama_archive_sha256=${ARES_OLLAMA_ARCHIVE_SHA256:-50539c5fe9bf85887733355098dcdb266b433cb8c73fa180713417e9ed6e42bb}
ollama_url=${ARES_OLLAMA_URL:-"https://github.com/ollama/ollama/releases/download/${ollama_version}/ollama-linux-amd64.tar.zst"}

models_dir=${ARES_AI_MODELS_DIR:-"${repo_root}/live/models"}
build_dir=${ARES_AI_BUILD_DIR:-"${repo_root}/live/.build/ai"}
archive_path="${build_dir}/downloads/ollama-linux-amd64-${ollama_version}.tar.zst"
extract_dir="${build_dir}/extract"
state_dir="${build_dir}/state"
server_log="${build_dir}/ollama-serve.log"
server_pid=

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    if [ -n "${server_pid}" ] && kill -0 "${server_pid}" 2>/dev/null; then
        kill "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM

[ "${model_name}" = "${expected_model}" ] || {
    fail "unsupported ARES_AI_MODEL '${model_name}'; expected '${expected_model}'"
}

for required in curl python3 tar unzstd sha256sum file; do
    command -v "${required}" >/dev/null 2>&1 || fail "${required} is required"
done

mkdir -p "${build_dir}/downloads" "${models_dir}" "${state_dir}"

if [ ! -s "${archive_path}" ]; then
    printf 'Downloading Ollama %s...\n' "${ollama_version}"
    curl -fL --retry 3 --retry-delay 5 \
        --output "${archive_path}.partial" \
        "${ollama_url}"
    mv -f "${archive_path}.partial" "${archive_path}"
fi

actual_archive_sha256=$(sha256sum "${archive_path}" | awk '{print $1}')
[ "${actual_archive_sha256}" = "${ollama_archive_sha256}" ] || {
    rm -f "${archive_path}"
    fail 'downloaded Ollama archive does not match the pinned SHA-256'
}

rm -rf -- "${extract_dir}"
mkdir -p "${extract_dir}"
unzstd -c "${archive_path}" | tar -xf - -C "${extract_dir}"

python3 - "${extract_dir}" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
for link in root.rglob("*"):
    if not link.is_symlink():
        continue
    resolved = link.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"unsafe symlink in Ollama archive: {link} -> {resolved}") from exc
PY

for generated in runtime store BUNDLE.json SHA256SUMS; do
    target="${models_dir}/${generated}"
    if [ -e "${target}" ]; then
        rm -rf -- "${target}"
    fi
done

mkdir -p "${models_dir}/runtime" "${models_dir}/store" "${state_dir}/home"
cp -aL "${extract_dir}/." "${models_dir}/runtime/"

[ -x "${models_dir}/runtime/bin/ollama" ] || {
    fail 'Ollama archive did not provide runtime/bin/ollama'
}
file "${models_dir}/runtime/bin/ollama" | grep -Eq 'ELF 64-bit LSB.*x86-64' || {
    fail 'runtime/bin/ollama is not an amd64 ELF executable'
}

port=$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)
ollama_host="127.0.0.1:${port}"
ollama_base_url="http://${ollama_host}"

printf 'Starting temporary Ollama on %s...\n' "${ollama_host}"
HOME="${state_dir}/home" \
OLLAMA_HOST="${ollama_host}" \
OLLAMA_MODELS="${models_dir}/store" \
    "${models_dir}/runtime/bin/ollama" serve >"${server_log}" 2>&1 &
server_pid=$!

for _ in $(seq 1 120); do
    if curl -fsS "${ollama_base_url}/api/tags" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        sed -n '1,160p' "${server_log}" >&2 || true
        fail 'temporary Ollama server exited before becoming ready'
    fi
    sleep 1
done

curl -fsS "${ollama_base_url}/api/tags" >/dev/null 2>&1 || {
    sed -n '1,160p' "${server_log}" >&2 || true
    fail 'temporary Ollama server did not become ready'
}

printf 'Pulling %s into the offline store...\n' "${model_name}"
HOME="${state_dir}/home" \
OLLAMA_HOST="${ollama_host}" \
OLLAMA_MODELS="${models_dir}/store" \
    "${models_dir}/runtime/bin/ollama" pull "${model_name}"

manifest_path="${models_dir}/store/manifests/registry.ollama.ai/library/qwen2.5/1.5b-instruct-q4_K_M"
[ -s "${manifest_path}" ] || fail 'Ollama did not materialize the expected model manifest'

python3 - "${ollama_version}" "${ollama_url}" "${ollama_archive_sha256}" "${model_name}" "${models_dir}/BUNDLE.json" <<'PY'
import json
import sys

version, runtime_source, runtime_sha256, model_name, output_path = sys.argv[1:]
bundle = {
    "schema_version": 1,
    "architecture": "amd64",
    "runtime": {
        "name": "ollama",
        "version": version,
        "source": runtime_source,
        "license": "MIT",
        "archive_sha256": runtime_sha256,
    },
    "model": {
        "name": model_name,
        "source": f"https://ollama.com/library/{model_name}",
        "upstream_source": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "license": "Apache-2.0",
    },
}
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(bundle, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

(
    cd "${models_dir}"
    {
        printf '%s\n' BUNDLE.json
        find runtime store -type f -print | LC_ALL=C sort
    } | while IFS= read -r file; do
        sha256sum "${file}"
    done > SHA256SUMS
    sha256sum --check --strict SHA256SUMS
)

printf 'Offline AI bundle is ready at %s\n' "${models_dir}"
