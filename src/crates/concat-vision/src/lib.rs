// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Jareer and Concat contributors

//! What the engine sees in a picture.
//!
//! A cutout takes a picture's background away without a key colour: a
//! model says, pixel by pixel, how likely each one is to be the person, and
//! the picture's alpha is multiplied by the answer. This crate is the whole
//! of that, in four parts:
//!
//! - [`mask`]: the answer itself, an eight-bit probability picture, with
//!   the sampling, softening and PNG form everything else uses.
//! - [`strokes`]: the corrections. A custom cutout paints brush strokes
//!   over the imported mask; this is where a stroke becomes pixels. A
//!   smart stroke names a thing rather than painting a disc: the region it
//!   answers is kept beside the masks, imported the same way they are.
//! - [`apply`]: the frame with its background gone. One function, called
//!   by the exporter and the monitor alike, so the file and the screen
//!   agree by construction.
//! - [`store`]: where masks live between runs. They are keyed by the media
//!   file and the source instant, and cached in the project folder like
//!   its waveforms, so a cutout is found once and travels with the edit.
//!
//! Nothing here finds a mask. This fork carries no inference: masks are
//! computed by a service outside the engine and imported into the store
//! (`concat-host`'s cutout module), which records the name of whatever
//! made them. The renderer reads masks and never infers; an export whose
//! cutouts have no masks is refused rather than rendered untreated.
//!
//! A mask is whatever shape its model answers in - square for the object
//! model, the picture's own aspect for the person model - and every
//! position in one is a fraction of the source picture: `(0, 0)` its
//! top-left, `(1, 1)` its bottom-right. Strokes are stored in the same
//! fractions. A crop, a flip or a change of output size therefore changes
//! nothing about a mask - the mapping from a decoded pixel back to a
//! source fraction is [`apply::Mapping`], and it is the one place those
//! are undone.

pub mod apply;
pub mod mask;
pub mod store;
pub mod strokes;

pub use apply::{Mapping, cut, highlight};
pub use mask::Mask;
pub use store::{MaskStore, mask_dir, region_dir};

/// Masks are kept this many times a second of source. Ten is where a
/// person's outline stops visibly lagging their movement, and where a
/// minute of footage is six hundred masks rather than eighteen hundred.
pub const MASK_RATE: u32 = 10;
