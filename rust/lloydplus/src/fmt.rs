//! Logging shim: `defmt` on firmware, `log` on a host, silence otherwise, so
//! every call site in the crate is identical in all three builds.

#[cfg(all(feature = "defmt", feature = "log"))]
compile_error!("enable either `defmt` or `log` logging, not both");

#[cfg(feature = "defmt")]
pub use defmt::{Debug2Format, debug, error, info, warn};

#[cfg(all(not(feature = "defmt"), feature = "log"))]
pub use log::{debug, error, info, warn};

#[cfg(not(any(feature = "defmt", feature = "log")))]
mod silent {
    macro_rules! noop {
        ($($arg:tt)*) => {{
            let _ = format_args!($($arg)*);
        }};
    }
    pub(crate) use noop as debug;
    pub(crate) use noop as error;
    pub(crate) use noop as info;
    pub(crate) use noop as warn;
}
#[cfg(not(any(feature = "defmt", feature = "log")))]
pub(crate) use silent::{debug, error, info, warn};

/// `defmt::Debug2Format` equivalent for the non-defmt paths: formats the
/// wrapped value with `{:?}` whatever the caller's format spec.
#[cfg(not(feature = "defmt"))]
pub struct Debug2Format<'a, T: core::fmt::Debug + ?Sized>(pub &'a T);

#[cfg(not(feature = "defmt"))]
impl<T: core::fmt::Debug + ?Sized> core::fmt::Debug for Debug2Format<'_, T> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        self.0.fmt(f)
    }
}

#[cfg(not(feature = "defmt"))]
impl<T: core::fmt::Debug + ?Sized> core::fmt::Display for Debug2Format<'_, T> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        self.0.fmt(f)
    }
}
