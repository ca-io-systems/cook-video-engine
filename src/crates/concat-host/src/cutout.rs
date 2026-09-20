// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors
// Modified 2026-09-20 for the Cook video engine fork: the on-device
// inference and the model downloads are removed; masks are imported.

//! Which masks a cutout still needs, and bringing them in.
//!
//! A clip with a cutout needs a mask for every source instant it shows, on
//! the grid `concat-vision` reads by: a still is one mask; footage is
//! [`concat_vision::MASK_RATE`] masks a second of source. This fork finds
//! none of them itself. It says which are missing ([`Cutouts::requests`],
//! [`Cutouts::missing`]) so that a caller can have them made elsewhere,
//! and it takes a finished set in ([`Cutouts::import`]), all of it or none
//! of it, recording the name of whatever made it.
//!
//! An export asks the same question before it renders and is refused while
//! any mask is missing: footage with a cutout is never rendered untreated.
//!
//! One import at a time through a [`SingleFlight`], like every long job the
//! host runs.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use concat_project::model::{MediaKind, Project, Subject};
use concat_vision::{Mask, MaskStore, mask_dir};

use crate::jobs::SingleFlight;

/// The grid masks are kept on, in milliseconds of source: an instant that
/// is a whole number of these is one a mask can be asked for.
pub const STEP_MS: u64 = 1000 / concat_vision::MASK_RATE as u64;

/// What one media file's cutout covers.
#[derive(Clone, Debug)]
pub struct MaskRequest {
    /// The project folder the masks are cached under.
    pub project: PathBuf,
    /// The media file the masks describe.
    pub media_path: String,
    /// A still: one mask answers for every instant.
    pub still: bool,
    /// What to keep.
    pub subject: Subject,
    /// The stretches of source, in seconds, that clips show.
    pub ranges: Vec<(f64, f64)>,
}

/// The import service: the one-job slot.
#[derive(Default)]
pub struct Cutouts {
    gate: Arc<SingleFlight>,
}

impl Cutouts {
    /// A service with nothing running.
    pub fn new() -> Cutouts {
        Cutouts::default()
    }

    /// Whether an import is running.
    pub fn is_busy(&self) -> bool {
        self.gate.is_busy()
    }

    /// What the active timeline's cutouts need: one request per media and
    /// subject, since two clips of one file that keep different things need
    /// different masks, each covering the stretches of source its clips
    /// show. Paired with the media's id, for a caller that keys its
    /// bookkeeping by it.
    pub fn requests(project: &Project, project_dir: &Path) -> Vec<(String, MaskRequest)> {
        let mut wanted: Vec<(String, MaskRequest)> = Vec::new();
        for clip in &project.active().clips {
            let Some(cutout) = clip.cutout.as_ref() else {
                continue;
            };
            if !clip.kind.is_visual() {
                continue;
            }
            let Some(media) = project.media_by_id(&clip.media_id) else {
                continue;
            };
            // The source the clip shows: its in-point, for as long as it
            // runs at its speed. A curve's mean is its speed, so this
            // covers a curved clip too.
            let range = (
                clip.source_start,
                clip.source_start + clip.duration * clip.speed.max(0.0625),
            );
            match wanted
                .iter_mut()
                .find(|(id, request)| *id == media.id && request.subject == cutout.subject)
            {
                Some((_, request)) => request.ranges.push(range),
                None => wanted.push((
                    media.id.clone(),
                    MaskRequest {
                        project: project_dir.to_path_buf(),
                        media_path: media.path.clone(),
                        still: media.kind == MediaKind::Image,
                        subject: cutout.subject,
                        ranges: vec![range],
                    },
                )),
            }
        }
        wanted
    }

    /// The source instants, in milliseconds, `request` has no mask for yet,
    /// ascending. A still with no mask is missing instant `0`.
    pub fn missing(request: &MaskRequest) -> Vec<u64> {
        let store = MaskStore::open(&mask_dir(
            &request.project,
            &request.media_path,
            request.subject,
        ));
        if request.still {
            return if store.is_empty() {
                vec![0]
            } else {
                Vec::new()
            };
        }
        let mut wanted: Vec<u64> = request
            .ranges
            .iter()
            .flat_map(|&(from, to)| store.missing(from, to))
            .collect();
        wanted.sort_unstable();
        wanted.dedup();
        wanted
    }

    /// How many instants `request` still needs.
    pub fn outstanding(request: &MaskRequest) -> usize {
        Self::missing(request).len()
    }

    /// Brings the masks in `frames` into the store `request` names: each is
    /// a source instant in milliseconds and the PNG whose first channel is
    /// the mask there. `source` names what made them. Every picture is read
    /// before anything is stored, and the set lands whole or not at all;
    /// see [`MaskStore::import`]. Returns how many masks were stored.
    pub fn import(
        &self,
        request: &MaskRequest,
        source: &str,
        frames: &[(u64, PathBuf)],
    ) -> Result<usize, String> {
        let _job = self.gate.begin("mask import")?;
        let mut masks: Vec<(u64, Mask)> = Vec::with_capacity(frames.len());
        for (millis, file) in frames {
            let bytes = std::fs::read(file)
                .map_err(|error| format!("could not read {}: {error}", file.display()))?;
            let mask = Mask::from_png(&bytes).ok_or_else(|| {
                format!("{} is not a PNG a mask can be read from", file.display())
            })?;
            // A still keeps its one mask at instant zero, whatever the
            // caller named it.
            masks.push((if request.still { 0 } else { *millis }, mask));
        }
        let mut store = MaskStore::open(&mask_dir(
            &request.project,
            &request.media_path,
            request.subject,
        ));
        store.import(source, &masks)
    }
}

/// The masks in `dir`, by the instant each file is named for: `<millis>.png`,
/// with or without leading zeros. Ascending. Files named anything else are
/// not masks and are left out.
pub fn frames_in(dir: &Path) -> Result<Vec<(u64, PathBuf)>, String> {
    let entries = std::fs::read_dir(dir)
        .map_err(|error| format!("could not read {}: {error}", dir.display()))?;
    let mut frames: Vec<(u64, PathBuf)> = entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name();
            let millis = name.to_str()?.strip_suffix(".png")?.parse::<u64>().ok()?;
            Some((millis, entry.path()))
        })
        .collect();
    frames.sort_unstable_by_key(|(millis, _)| *millis);
    frames.dedup_by_key(|(millis, _)| *millis);
    Ok(frames)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "concat-host-cutout-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("a scratch directory");
        dir
    }

    fn request(project: &Path, still: bool) -> MaskRequest {
        MaskRequest {
            project: project.to_path_buf(),
            media_path: "clip.mp4".to_owned(),
            still,
            subject: Subject::Person,
            ranges: vec![(0.0, 0.2)],
        }
    }

    #[test]
    fn footage_is_missing_every_instant_until_masks_are_imported() {
        let project = scratch("missing");
        let request = request(&project, false);
        assert_eq!(Cutouts::missing(&request), vec![0, 100, 200]);

        let incoming = project.join("incoming");
        std::fs::create_dir_all(&incoming).expect("a directory");
        for millis in [0u64, 100, 200] {
            std::fs::write(
                incoming.join(format!("{millis:09}.png")),
                Mask::filled(4, 4, 255).to_png(),
            )
            .expect("writes");
        }
        std::fs::write(incoming.join("notes.txt"), "not a mask").expect("writes");
        let frames = frames_in(&incoming).expect("reads");
        assert_eq!(frames.len(), 3);

        let cutouts = Cutouts::new();
        assert_eq!(cutouts.import(&request, "cloud-model", &frames), Ok(3));
        assert_eq!(Cutouts::outstanding(&request), 0);
        let _ = std::fs::remove_dir_all(&project);
    }

    #[test]
    fn a_picture_that_is_not_a_png_stores_nothing() {
        let project = scratch("bad");
        let request = request(&project, false);
        let good = project.join("000000000.png");
        let bad = project.join("000000100.png");
        std::fs::write(&good, Mask::filled(4, 4, 255).to_png()).expect("writes");
        std::fs::write(&bad, b"not a png").expect("writes");
        let cutouts = Cutouts::new();
        assert!(
            cutouts
                .import(&request, "cloud-model", &[(0, good), (100, bad)])
                .is_err()
        );
        assert_eq!(Cutouts::missing(&request), vec![0, 100, 200]);
        let _ = std::fs::remove_dir_all(&project);
    }

    #[test]
    fn a_still_needs_one_mask_and_keeps_it_at_zero() {
        let project = scratch("still");
        let request = request(&project, true);
        assert_eq!(Cutouts::missing(&request), vec![0]);
        let file = project.join("000004200.png");
        std::fs::write(&file, Mask::filled(4, 4, 255).to_png()).expect("writes");
        let cutouts = Cutouts::new();
        assert_eq!(
            cutouts.import(&request, "cloud-model", &[(4200, file)]),
            Ok(1)
        );
        assert!(Cutouts::missing(&request).is_empty());
        let _ = std::fs::remove_dir_all(&project);
    }
}
