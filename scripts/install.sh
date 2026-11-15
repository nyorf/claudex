#!/usr/bin/env bash
set -euo pipefail

repository="https://github.com/nyorf/claudex"
install_dir="${HOME}/.local/opt/claudex"
bin_dir="${HOME}/.local/bin"
link_path="${bin_dir}/claudex"
man_dir="${HOME}/.local/share/man/man1"
man_path="${man_dir}/claudex.1"
backup="${install_dir}.previous"
lock="${install_dir}.lock"
archive_arg=""
choice=""
script_path="${BASH_SOURCE[0]-}"

fail() { printf 'Claudex installer: %s\n' "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update) [[ -z $choice ]] || fail 'Select update only once.'; choice=2; shift ;;
        --archive)
            [[ $# -ge 2 && -z $archive_arg ]] || fail 'Use --archive PATH to select one archive.'
            archive_arg=$2
            shift 2
            ;;
        *) fail 'Use --update or --archive PATH.' ;;
    esac
done

if [[ -z $choice ]]; then
    printf 'Claudex installer\n\n1) Install\n2) Update\n3) Remove\n0) Exit\n\nChoice: '
    if [[ -z $script_path || $script_path == main || $script_path == /dev/stdin ]]; then
        [[ -r /dev/tty ]] || fail 'Run this installer in a terminal.'
        IFS= read -r choice < /dev/tty
    else
        IFS= read -r choice || fail 'No action was selected.'
    fi
fi
case "$choice" in
    0) exit 0 ;;
    1|2|3) ;;
    *) fail 'Choose 1, 2, 3, or 0.' ;;
esac

mkdir -p "$(dirname "$install_dir")" "$bin_dir"
[[ ! -L $install_dir && ! -L $backup && ! -L $lock ]] || fail 'An installer path is a symbolic link.'
mkdir "$lock" 2>/dev/null || fail 'Another installation may be running. Remove the installer lock if it is stale.'
stage=""
installed_new=false
link_created=false
man_created=false
committed=false
cleanup() {
    if [[ $installed_new == true && $committed == false && -f $install_dir/.claudex-install ]]; then
        rm -rf "$install_dir" || true
        if [[ $link_created == true && -L $link_path ]]; then rm "$link_path" || true; fi
        if [[ $man_created == true && -L $man_path ]]; then rm "$man_path" || true; fi
    fi
    if [[ -d $backup && ! -e $install_dir && -f $backup/.claudex-install ]]; then
        mv "$backup" "$install_dir" || true
    fi
    if [[ -n $stage && -d $stage ]]; then rm -rf "$stage"; fi
    rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT
if [[ -d $backup ]]; then
    [[ -f $backup/.claudex-install ]] || fail 'The previous installation cannot be verified.'
    if [[ -e $install_dir ]]; then
        [[ -f $install_dir/.claudex-install ]] || fail 'The installed bundle cannot be verified.'
        rm -rf "$backup"
    else
        mv "$backup" "$install_dir"
    fi
fi

if [[ -e $install_dir ]]; then
    [[ -d $install_dir && -f $install_dir/.claudex-install ]] || fail 'The installation directory belongs to another installation.'
fi
if [[ -e $link_path || -L $link_path ]]; then
    [[ -L $link_path && $(readlink "$link_path") == "$install_dir/claudex" ]] || fail 'The claudex command is already in use in ~/.local/bin.'
fi
if [[ $choice != 3 && ( -e $man_path || -L $man_path ) ]]; then
    [[ -L $man_path && $(readlink "$man_path") == "$install_dir/claudex.1" ]] || fail 'The claudex manual page is already in use.'
fi

case "$choice" in
    1) [[ ! -e $install_dir ]] || fail 'Claudex is already installed. Choose Update.' ;;
    2) [[ -d $install_dir ]] || fail 'Claudex is not installed. Choose Install.' ;;
    3)
        [[ -d $install_dir ]] || fail 'Claudex is not installed by this installer.'
        [[ ! -L $link_path ]] || rm "$link_path"
        if [[ -L $man_path && $(readlink "$man_path") == "$install_dir/claudex.1" ]]; then
            rm "$man_path"
        fi
        rm -rf "$install_dir"
        printf 'Removed Claudex. Your profiles and configuration remain on this computer.\n'
        exit 0
        ;;
esac

case "$(uname -s)" in
    Darwin) system=darwin ;;
    Linux) system=linux ;;
    *) fail 'This installer supports macOS and Linux.' ;;
esac
case "$(uname -m)" in
    x86_64|amd64) architecture=x86_64 ;;
    arm64|aarch64) architecture=arm64 ;;
    *) fail 'This processor is not supported.' ;;
esac

stage=$(mktemp -d "$(dirname "$install_dir")/.claudex-install.XXXXXXXX")
if [[ -n $archive_arg ]]; then
    [[ -f $archive_arg && -f ${archive_arg}.sha256 ]] || fail 'The local archive and its .sha256 file are required.'
    archive_name=$(basename "$archive_arg")
    cp "$archive_arg" "$stage/$archive_name"
    cp "${archive_arg}.sha256" "$stage/checksum"
else
    command -v curl >/dev/null || fail 'Install curl and run the installer again.'
    latest=$(curl -fsSL --proto '=https' --proto-redir '=https' -o /dev/null -w '%{url_effective}' "$repository/releases/latest") || fail 'Could not find the latest GitHub release.'
    tag=${latest#"$repository/releases/tag/"}
    [[ $latest == "$repository/releases/tag/$tag" && $tag =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'The latest release tag is invalid.'
    version=${tag#v}
    archive_name="claudex-${version}-${system}-${architecture}.tar.gz"
    release_url="$repository/releases/download/$tag"
    curl -fsSL --proto '=https' --proto-redir '=https' "$release_url/$archive_name" -o "$stage/$archive_name" || fail 'The release archive is unavailable for this system.'
    curl -fsSL --proto '=https' --proto-redir '=https' "$release_url/$archive_name.sha256" -o "$stage/checksum" || fail 'The release checksum is unavailable.'
fi
[[ $archive_name =~ ^claudex-[0-9]+\.[0-9]+\.[0-9]+-${system}-${architecture}\.tar\.gz$ ]] || fail 'The archive name does not match this system.'

read -r expected checksum_name < "$stage/checksum" || fail 'The checksum file is invalid.'
[[ $expected =~ ^[a-fA-F0-9]{64}$ && $checksum_name == "$archive_name" ]] || fail 'The checksum file does not match the archive.'
if command -v sha256sum >/dev/null; then
    actual=$(sha256sum "$stage/$archive_name")
elif command -v shasum >/dev/null; then
    actual=$(shasum -a 256 "$stage/$archive_name")
else
    fail 'Install sha256sum or shasum and run the installer again.'
fi
actual=${actual%% *}
actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
[[ $actual == "$expected" ]] || fail 'The archive checksum did not match.'

tar -tzf "$stage/$archive_name" > "$stage/entries" || fail 'The archive could not be read.'
while IFS= read -r entry; do
    [[ $entry == claudex || $entry == claudex/* ]] || fail 'The archive has an unexpected layout.'
    [[ "/$entry/" != *'/../'* ]] || fail 'The archive contains an unsafe path.'
done < "$stage/entries"
tar -xzf "$stage/$archive_name" -C "$stage" || fail 'The archive could not be extracted.'
[[ -f $stage/claudex/claudex && -x $stage/claudex/claudex && -d $stage/claudex/_internal ]] || fail 'The release bundle is incomplete.'
[[ -f $stage/claudex/claudex.1 ]] || fail 'The release bundle is missing its manual page.'
version=${archive_name#claudex-}
version=${version%%-*}
reported=$("$stage/claudex/claudex" --version) || fail 'The release executable could not report its version.'
[[ $reported == "claudex $version" || $reported == "claudex $version ($repository)" ]] || fail 'The release executable did not report the expected version.'
if [[ -f $script_path ]]; then
    cp "$script_path" "$stage/claudex/install.sh"
else
    curl -fsSL --proto '=https' --proto-redir '=https' "$release_url/install.sh" -o "$stage/claudex/install.sh" || fail 'Could not save the installer for later use.'
fi
printf 'Claudex release installer\n' > "$stage/claudex/.claudex-install"
if [[ -d $install_dir ]]; then mv "$install_dir" "$backup"; fi
mv "$stage/claudex" "$install_dir"
installed_new=true
if [[ ! -L $link_path ]]; then
    ln -s "$install_dir/claudex" "$link_path"
    link_created=true
fi
if [[ ! -L $man_path ]]; then
    mkdir -p "$man_dir"
    ln -s "$install_dir/claudex.1" "$man_path"
    man_created=true
fi
committed=true
if [[ -d $backup ]]; then rm -rf "$backup"; fi
printf 'Claudex %s is installed.\n' "$version"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) printf 'Add %s to your PATH to run claudex from a terminal.\n' "$bin_dir" ;;
esac
printf 'To update or remove it later, run bash "%s/install.sh".\n' "$install_dir"
