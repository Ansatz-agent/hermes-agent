//! Verification and installation of the source snapshot shipped with Hermes
//! Setup.
//!
//! The archive is deliberately a plain tar.gz.  Its commit and digest are
//! release metadata, not an attempt to hide the Python source.  This module
//! only provides integrity checks and an atomic source-directory swap so a
//! failed dependency install cannot leave a half-extracted runtime behind.

use anyhow::{anyhow, Context, Result};
use flate2::read::GzDecoder;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use tar::Archive;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

const MANIFEST_SCHEMA_VERSION: u32 = 1;
const SOURCE_MARKER_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PayloadFile {
    pub file: String,
    pub size: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PayloadManifest {
    pub schema_version: u32,
    pub commit: String,
    pub branch: Option<String>,
    pub archive: PayloadFile,
    pub installer: PayloadFile,
}

#[derive(Debug, Clone)]
pub struct BundledPayload {
    pub root: PathBuf,
    pub archive_path: PathBuf,
    pub installer_path: PathBuf,
    pub manifest: PayloadManifest,
}

impl BundledPayload {
    /// Associated wrapper kept for callers that carry the discovered payload
    /// type through the bootstrap/update flow.
    pub fn discover(
        resource_root: &Path,
        installer_name: &str,
        expected_commit: Option<&str>,
    ) -> Result<Option<Self>> {
        discover(resource_root, installer_name, expected_commit)
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SourceMarker {
    schema_version: u32,
    commit: String,
    archive_sha256: String,
}

/// A transaction covering the active source tree.  If the caller returns an
/// error before `finalize`, Drop restores the previous tree synchronously.
/// All operations are local renames, so this is safe even when the async
/// bootstrap task is cancelled while a stage is running.
pub struct SourceTransaction {
    active_root: PathBuf,
    backup_root: Option<PathBuf>,
    committed: bool,
}

impl SourceTransaction {
    pub fn finalize(mut self) -> Result<()> {
        if let Some(backup) = self.backup_root.take() {
            if backup.exists() {
                fs::remove_dir_all(&backup)
                    .with_context(|| format!("removing source backup {}", backup.display()))?;
            }
        }
        self.committed = true;
        Ok(())
    }
}

impl Drop for SourceTransaction {
    fn drop(&mut self) {
        if self.committed {
            return;
        }

        if let Some(backup) = self.backup_root.take() {
            let _ = fs::remove_dir_all(&self.active_root);
            if let Err(err) = fs::rename(&backup, &self.active_root) {
                tracing::error!(
                    ?err,
                    active = %self.active_root.display(),
                    backup = %backup.display(),
                    "failed to roll back bundled source tree"
                );
            }
        }
    }
}

fn is_sha(value: &str) -> bool {
    value.len() == 40 && value.chars().all(|c| c.is_ascii_hexdigit())
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1024 * 64];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn verify_file(path: &Path, metadata: &PayloadFile, label: &str) -> Result<()> {
    let link_item = fs::symlink_metadata(path)
        .with_context(|| format!("reading {label} {}", path.display()))?;
    if link_item.file_type().is_symlink() {
        return Err(anyhow!("bundled {label} must not be a symbolic link: {}", path.display()));
    }
    let item = fs::metadata(path).with_context(|| format!("reading {label} {}", path.display()))?;
    if !item.is_file() {
        return Err(anyhow!("bundled {label} is not a regular file: {}", path.display()));
    }
    if item.len() != metadata.size {
        return Err(anyhow!(
            "bundled {label} size mismatch: expected {}, got {}",
            metadata.size,
            item.len()
        ));
    }
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(&metadata.sha256) {
        return Err(anyhow!(
            "bundled {label} SHA-256 mismatch: expected {}, got {}",
            metadata.sha256,
            actual
        ));
    }
    Ok(())
}

/// Discover and verify a payload under a Tauri resource directory.  An empty
/// resource directory returns `Ok(None)` for debug/dev builds; a partially
/// present payload is always an error because silently falling back to a
/// network source would make a broken release non-deterministic.
pub fn discover(
    resource_root: &Path,
    installer_name: &str,
    expected_commit: Option<&str>,
) -> Result<Option<BundledPayload>> {
    let manifest_path = resource_root.join("payload-manifest.json");
    let archive_path = resource_root.join("hermes-backend.tar.gz");
    let installer_path = resource_root.join(installer_name);
    let present = [manifest_path.exists(), archive_path.exists(), installer_path.exists()]
        .into_iter()
        .filter(|value| *value)
        .count();

    if present == 0 {
        return Ok(None);
    }
    if present != 3 {
        return Err(anyhow!(
            "bundled payload is incomplete under {}",
            resource_root.display()
        ));
    }

    let manifest: PayloadManifest = serde_json::from_slice(
        &fs::read(&manifest_path).with_context(|| format!("reading {}", manifest_path.display()))?,
    )
    .with_context(|| format!("parsing {}", manifest_path.display()))?;

    if manifest.schema_version != MANIFEST_SCHEMA_VERSION {
        return Err(anyhow!(
            "unsupported bundled payload schema {}; expected {}",
            manifest.schema_version,
            MANIFEST_SCHEMA_VERSION
        ));
    }
    if !is_sha(&manifest.commit) || manifest.commit.chars().all(|c| c == '0') {
        return Err(anyhow!("bundled payload has an invalid commit"));
    }
    if let Some(expected) = expected_commit.filter(|value| is_sha(value)) {
        if !manifest.commit.eq_ignore_ascii_case(expected) {
            return Err(anyhow!(
                "bundled payload commit {} does not match installer commit {}",
                manifest.commit,
                expected
            ));
        }
    }
    if manifest.archive.file != "hermes-backend.tar.gz" {
        return Err(anyhow!("bundled payload archive filename is invalid"));
    }
    if manifest.installer.file != installer_name {
        return Err(anyhow!("bundled payload installer filename is invalid"));
    }

    verify_file(&archive_path, &manifest.archive, "source archive")?;
    verify_file(&installer_path, &manifest.installer, "install script")?;

    Ok(Some(BundledPayload {
        root: resource_root.to_path_buf(),
        archive_path,
        installer_path,
        manifest,
    }))
}

/// Compare the verified resource payload with the marker in the active source
/// tree. `Ok(None)` means this is a development/non-bundled launch and keeps
/// the pre-existing launcher behavior; `Ok(Some(false))` means a newer Setup
/// payload is present and the installer UI must run even when Hermes is already
/// installed.
pub fn matches_active_source(
    resource_root: &Path,
    installer_name: &str,
    active_root: &Path,
) -> Result<Option<bool>> {
    let Some(payload) = discover(resource_root, installer_name, None)? else {
        return Ok(None);
    };
    let Some(marker) = source_marker(active_root) else {
        return Ok(Some(false));
    };
    Ok(Some(
        marker.schema_version == SOURCE_MARKER_SCHEMA_VERSION
            && marker.commit.eq_ignore_ascii_case(&payload.manifest.commit)
            && marker
                .archive_sha256
                .eq_ignore_ascii_case(&payload.manifest.archive.sha256)
            && required_source_files(active_root),
    ))
}

fn source_marker(path: &Path) -> Option<SourceMarker> {
    let raw = fs::read(path.join(".hermes-bundled-source.json")).ok()?;
    serde_json::from_slice(&raw).ok()
}

fn validate_archive_path(path: &Path) -> Result<Vec<Component<'_>>> {
    let components: Vec<_> = path.components().collect();
    if components.is_empty() {
        return Err(anyhow!("bundled archive contains an empty path"));
    }
    for component in &components {
        match component {
            Component::Normal(_) => {}
            _ => return Err(anyhow!("bundled archive contains unsafe path {path:?}")),
        }
    }
    match components.first() {
        Some(Component::Normal(name)) if *name == std::ffi::OsStr::new("hermes-agent") => {}
        _ => return Err(anyhow!("bundled archive entry is missing hermes-agent prefix: {path:?}")),
    }
    Ok(components)
}

fn extract_archive(archive_path: &Path, staging_root: &Path) -> Result<()> {
    let archive = fs::File::open(archive_path)
        .with_context(|| format!("opening source archive {}", archive_path.display()))?;
    let decoder = GzDecoder::new(archive);
    let mut tar = Archive::new(decoder);

    for item in tar.entries()? {
        let mut entry = item.context("reading bundled source archive entry")?;
        // GNU/POSIX tar writers may emit a global PAX header named
        // `pax_global_header`. It carries archive metadata rather than a
        // filesystem object and therefore does not belong under the required
        // `hermes-agent/` source prefix. Ignore it before validating paths.
        let entry_type = entry.header().entry_type();
        if entry_type.is_pax_global_extensions() || entry_type.is_pax_local_extensions() {
            continue;
        }
        let path = entry.path()?.into_owned();
        let components = validate_archive_path(&path)?;
        let relative = components.iter().skip(1).fold(PathBuf::new(), |mut out, component| {
            if let Component::Normal(name) = component {
                out.push(name);
            }
            out
        });
        if relative.as_os_str().is_empty() {
            continue;
        }

        if entry_type.is_symlink() || entry_type.is_hard_link() {
            return Err(anyhow!("bundled archive contains a link: {path:?}"));
        }

        let destination = staging_root.join(relative);
        if entry_type.is_dir() {
            fs::create_dir_all(&destination)?;
        } else if entry_type.is_file() {
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)?;
            }
            entry.unpack(&destination).with_context(|| {
                format!("extracting bundled source entry {}", destination.display())
            })?;
        } else {
            return Err(anyhow!("bundled archive contains unsupported entry: {path:?}"));
        }
    }
    Ok(())
}

fn required_source_files(root: &Path) -> bool {
    [
        "pyproject.toml",
        "hermes_cli/main.py",
        "tools/sensevoice_stt.py",
        "apps/desktop/package.json",
        "apps/bootstrap-installer/package.json",
    ]
    .iter()
    .all(|relative| root.join(relative).is_file())
}

/// Atomically install the payload into `active_root`. Existing managed
/// bundles are reused when their commit and archive digest already match.
/// Existing Git checkouts are treated as a one-time migration source and are
/// moved to a rollback backup before the bundled tree is promoted.
pub fn prepare_source(
    payload: &BundledPayload,
    active_root: &Path,
    hermes_home: &Path,
) -> Result<SourceTransaction> {
    if active_root.parent() != Some(hermes_home) {
        return Err(anyhow!("bundled source install directory must be a direct child of Hermes home"));
    }
    fs::create_dir_all(hermes_home)
        .with_context(|| format!("creating Hermes home {}", hermes_home.display()))?;
    if active_root.exists() && fs::symlink_metadata(active_root)?.file_type().is_symlink() {
        return Err(anyhow!("refusing to replace symlinked install directory {}", active_root.display()));
    }

    // Use the same durable backup name as the Electron bootstrap path. This
    // lets either updater recover a process that stopped after the old tree
    // was moved aside but before the new tree was finalized.
    let active_name = active_root
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("hermes-agent");
    let durable_backup = hermes_home.join(format!(".{active_name}-bundled-backup"));
    if durable_backup.exists() {
        if !active_root.exists() {
            fs::rename(&durable_backup, active_root)
                .context("restoring bundled source backup before retry")?;
        } else if let Some(marker) = source_marker(active_root) {
            if marker.commit.eq_ignore_ascii_case(&payload.manifest.commit)
                && marker.archive_sha256.eq_ignore_ascii_case(&payload.manifest.archive.sha256)
                && required_source_files(active_root)
            {
                fs::remove_dir_all(&durable_backup)
                    .context("removing finalized bundled source backup")?;
            } else {
                return Err(anyhow!(
                    "refusing bundled-source recovery: active tree and backup both exist"
                ));
            }
        } else {
            return Err(anyhow!(
                "refusing bundled-source recovery: active tree and backup both exist"
            ));
        }
    }

    if let Some(marker) = source_marker(active_root) {
        if marker.schema_version == SOURCE_MARKER_SCHEMA_VERSION
            && marker.commit.eq_ignore_ascii_case(&payload.manifest.commit)
            && marker.archive_sha256.eq_ignore_ascii_case(&payload.manifest.archive.sha256)
            && required_source_files(active_root)
        {
            return Ok(SourceTransaction {
                active_root: active_root.to_path_buf(),
                backup_root: None,
                committed: false,
            });
        }
    }

    let staging = hermes_home.join(format!(".hermes-agent-staging-{}", std::process::id()));
    if staging.exists() {
        fs::remove_dir_all(&staging).context("removing stale bundled source staging directory")?;
    }
    fs::create_dir_all(&staging)?;

    let result = (|| -> Result<()> {
        extract_archive(&payload.archive_path, &staging)?;
        if !required_source_files(&staging) {
            return Err(anyhow!("bundled source is missing required runtime files"));
        }
        let scripts = staging.join("scripts");
        fs::create_dir_all(&scripts)?;
        let installer_target = scripts.join(&payload.manifest.installer.file);
        fs::copy(&payload.installer_path, &installer_target)
            .context("copying bundled install script into source tree")?;
        #[cfg(unix)]
        {
            let mut permissions = fs::metadata(&installer_target)?.permissions();
            permissions.set_mode(0o755);
            fs::set_permissions(&installer_target, permissions)?;
        }

        let marker = serde_json::json!({
            "schemaVersion": SOURCE_MARKER_SCHEMA_VERSION,
            "commit": payload.manifest.commit,
            "archiveSha256": payload.manifest.archive.sha256,
            "installedAt": format!("{:?}", std::time::SystemTime::now()),
        });
        fs::write(staging.join(".hermes-bundled-source.json"), serde_json::to_vec_pretty(&marker)?)?;
        fs::write(staging.join(".hermes_build_sha"), format!("{}\n", payload.manifest.commit))?;
        Ok(())
    })();

    if let Err(err) = result {
        let _ = fs::remove_dir_all(&staging);
        return Err(err);
    }

    let backup = if active_root.exists() {
        let candidate = durable_backup.clone();
        if candidate.exists() {
            let _ = fs::remove_dir_all(&staging);
            return Err(anyhow!("bundled source backup already exists: {}", candidate.display()));
        }
        if let Err(err) = fs::rename(active_root, &candidate) {
            let _ = fs::remove_dir_all(&staging);
            return Err(err).with_context(|| {
                format!("moving current source to backup {}", candidate.display())
            });
        }
        Some(candidate)
    } else {
        None
    };

    if let Err(err) = fs::rename(&staging, active_root) {
        if let Some(backup_path) = &backup {
            let _ = fs::rename(backup_path, active_root);
        }
        let _ = fs::remove_dir_all(&staging);
        return Err(err).with_context(|| format!("promoting bundled source to {}", active_root.display()));
    }

    Ok(SourceTransaction { active_root: active_root.to_path_buf(), backup_root: backup, committed: false })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_prefixed_relative_archive_paths() {
        assert!(validate_archive_path(Path::new("hermes-agent/pyproject.toml")).is_ok());
        assert!(validate_archive_path(Path::new("../pyproject.toml")).is_err());
        assert!(validate_archive_path(Path::new("hermes-agent/../../outside")).is_err());
        assert!(validate_archive_path(Path::new("other-root/file")).is_err());
    }

    #[test]
    fn validates_real_commit_shape() {
        assert!(is_sha(&"a".repeat(40)));
        assert!(!is_sha(&"a".repeat(39)));
        assert!(!is_sha(&"g".repeat(40)));
    }
}
