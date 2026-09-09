#!/usr/bin/env bash
set -Eeuo pipefail

qemu_tag="v10.0.2-utm"
qemu_commit="37ba092d59aff24900dfd0d5e01d4ed68441ba07"
utm_tag="v4.7.5"
install_root="${ALE_DARWIN_QEMU_ROOT:-$HOME/.cache/ale/darwin-qemu/$qemu_tag}"
source_root="${ALE_DARWIN_QEMU_SOURCE_ROOT:-$HOME/.cache/ale/sources/qemu-$qemu_tag}"
build_root="$source_root/build-ale-darwin"
repository="$(cd "$(dirname "$0")/.." && pwd)"

for binary in git curl uv ninja pkg-config bunzip2 shasum; do
  command -v "$binary" >/dev/null || {
    echo "$binary is required (brew install qemu meson ninja pkgconf)" >&2
    exit 69
  }
done

if [[ ! -d "$source_root/.git" ]]; then
  mkdir -p "$(dirname "$source_root")"
  git clone --depth 1 --branch "$qemu_tag" https://github.com/utmapp/qemu.git "$source_root"
fi
actual_commit="$(git -C "$source_root" rev-parse HEAD)"
[[ "$actual_commit" == "$qemu_commit" ]] || {
  echo "unexpected UTM QEMU commit in $source_root: $actual_commit" >&2
  exit 65
}

python_path="$(cd "$repository" && uv run python -c 'import sys; print(sys.executable)')"
ln -sf "$python_path" "$source_root/.ale-python"
mkdir -p "$build_root"
if [[ ! -f "$build_root/build.ninja" ]]; then
  (
    cd "$build_root"
    ../configure \
      --python="$source_root/.ale-python" \
      --target-list=aarch64-softmmu \
      --enable-hvf \
      --enable-slirp \
      --disable-docs \
      --disable-cocoa \
      --disable-sdl \
      --disable-gtk \
      --disable-opengl \
      --disable-spice \
      --disable-vnc \
      --disable-tools \
      --disable-guest-agent \
      --disable-debug-info
  )
fi
ninja -C "$build_root" qemu-system-aarch64

bin_dir="$install_root/bin"
data_dir="$install_root/share/qemu"
mkdir -p "$bin_dir" "$data_dir"
cp "$build_root/qemu-system-aarch64" "$bin_dir/qemu-system-aarch64-utm.new"
mv "$bin_dir/qemu-system-aarch64-utm.new" "$bin_dir/qemu-system-aarch64-utm"
cp "$source_root/pc-bios/vgabios-ramfb.bin" "$data_dir/vgabios-ramfb.bin"
cp "$source_root/pc-bios/efi-virtio.rom" "$data_dir/efi-virtio.rom"
bunzip2 -kc "$source_root/pc-bios/edk2-aarch64-code.fd.bz2" \
  > "$data_dir/edk2-aarch64-code.fd"

variables_archive="$source_root/pc-bios/edk2-arm-vars-utm-$utm_tag.fd.bz2"
curl -fsSL \
  "https://raw.githubusercontent.com/utmapp/UTM/$utm_tag/patches/data/qemu-10.0.2-utm/pc-bios/edk2-arm-vars.fd.bz2" \
  -o "$variables_archive"
bunzip2 -kc "$variables_archive" > "$data_dir/edk2-arm-vars.fd"

code_sha="$(shasum -a 256 "$data_dir/edk2-aarch64-code.fd" | awk '{print $1}')"
variables_sha="$(shasum -a 256 "$data_dir/edk2-arm-vars.fd" | awk '{print $1}')"
[[ "$code_sha" == "ee769c4bf42a5350d33345fc1b16d00156fe0c25fb3c68debd003bd844e4e3fa" ]] || {
  echo "unexpected UTM aarch64 firmware digest: $code_sha" >&2
  exit 65
}
[[ "$variables_sha" == "7b0a7f26192011e6e98c770694269b40f8b70620ca58fc4973a232fb223600d5" ]] || {
  echo "unexpected UTM ARM variables digest: $variables_sha" >&2
  exit 65
}
device_help="$("$bin_dir/qemu-system-aarch64-utm" -device help)"
grep -q 'name "virtio-ramfb"' <<< "$device_help" || {
  echo "built QEMU does not provide virtio-ramfb" >&2
  exit 70
}
codesign --verify "$bin_dir/qemu-system-aarch64-utm"

printf '%s\n' "$qemu_tag" "$qemu_commit" > "$install_root/VERSION"
echo "installed headless Darwin QEMU at $install_root"
