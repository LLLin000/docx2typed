//! Byte-level OOXML tag scanning (issue #58): the Rust mirror of
//! `scripts/xml_walker.py`, the single owner of the raw-XML token
//! discipline. Every structural pass in the recursive-prose pipeline
//! (paragraph locators, leaf ranges, opaque interiors) consumes `Tag`
//! tokens with byte offsets — never str offsets (CJK text is multi-byte
//! and would corrupt slicing).
//!
//! Hazards mirrored from the Python discipline:
//! - comment / CDATA / PI tokens are never treated as tags,
//! - self-closing detection (`<w:p/>`),
//! - namespace-prefix splitting (`w:p` -> local name `p`; matching is
//!   prefix-agnostic),
//! - nesting-safe scan: the caller owns the stack and nesting policy.

use crate::CoreError;

/// One structural token in the raw bytes (mirror of `Tag`).
#[derive(Clone, Debug)]
pub struct Tag {
    /// Local name, prefix stripped ("p" for "w:p").
    pub name: String,
    /// QName exactly as written ("w:p").
    pub raw_name: String,
    /// Closing tag (`</w:p>`).
    pub closing: bool,
    /// Self-closing tag (`<w:p/>`).
    pub self_closing: bool,
    /// Byte offset of '<'.
    pub start: usize,
    /// Byte offset just past '>'.
    pub end: usize,
}

impl Tag {
    /// The token bytes (identical to the source slice).
    pub fn bytes<'a>(&self, xml: &'a [u8]) -> &'a [u8] {
        &xml[self.start..self.end]
    }
}

/// Find the byte index of `needle` starting at or after `from`.
fn find_bytes(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from > haystack.len() {
        return None;
    }
    haystack[from..]
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|index| from + index)
}

/// Scan one `<...>`-shaped token: from `start` (the '<'), the token ends at
/// the first '>' outside a quoted attribute value (`"..."` or `'...'`).
/// Mirrors the final alternation of Python's `_TAG_RE`.
fn scan_angle_token(xml: &[u8], start: usize) -> Option<usize> {
    let mut index = start + 1;
    while index < xml.len() {
        match xml[index] {
            b'"' => {
                index += 1;
                while index < xml.len() && xml[index] != b'"' {
                    index += 1;
                }
                index += 1;
            }
            b'\'' => {
                index += 1;
                while index < xml.len() && xml[index] != b'\'' {
                    index += 1;
                }
                index += 1;
            }
            b'>' => return Some(index + 1),
            _ => index += 1,
        }
    }
    None
}

/// Classify one raw token; `None` for comments, PIs, and CDATA (and for
/// tokens that fail the start/close tag grammar — Python skips those
/// silently). Mirrors `parse_tag`.
fn parse_tag(token: &[u8], start: usize, end: usize) -> Option<Tag> {
    if token.starts_with(b"<!--") || token.starts_with(b"<?") || token.starts_with(b"<!") {
        return None;
    }
    let mut index = 1;
    while index < token.len()
        && (token[index] == b' '
            || token[index] == b'\t'
            || token[index] == b'\r'
            || token[index] == b'\n')
    {
        index += 1;
    }
    if index < token.len() && token[index] == b'/' {
        // Closing tag: `</\s*name\s*>` fullmatch.
        index += 1;
        while index < token.len()
            && (token[index] == b' '
                || token[index] == b'\t'
                || token[index] == b'\r'
                || token[index] == b'\n')
        {
            index += 1;
        }
        let name_start = index;
        while index < token.len() && is_name_byte(token[index]) {
            index += 1;
        }
        if index == name_start {
            return None;
        }
        let raw_name = std::str::from_utf8(&token[name_start..index])
            .ok()?
            .to_string();
        while index < token.len()
            && (token[index] == b' '
                || token[index] == b'\t'
                || token[index] == b'\r'
                || token[index] == b'\n')
        {
            index += 1;
        }
        if index != token.len() - 1 || token[index] != b'>' {
            return None;
        }
        return Some(Tag {
            name: local_name(&raw_name),
            raw_name,
            closing: true,
            self_closing: false,
            start,
            end,
        });
    }
    // Opening tag: `<\s*name(?:...)*?/?>` fullmatch.
    let name_start = index;
    while index < token.len() && is_name_byte(token[index]) {
        index += 1;
    }
    if index == name_start {
        return None;
    }
    let raw_name = std::str::from_utf8(&token[name_start..index])
        .ok()?
        .to_string();
    let self_closing = token
        .iter()
        .rev()
        .find(|byte| !byte.is_ascii_whitespace())
        .map(|byte| *byte == b'>')
        .unwrap_or(false)
        && token[token.len() - 1] == b'>'
        && token[token.len() - 2] == b'/';
    Some(Tag {
        name: local_name(&raw_name),
        raw_name,
        closing: false,
        self_closing,
        start,
        end,
    })
}

fn is_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b':' | b'-')
}

/// Local name of a qname (`w:p` -> `p`; a bare name stays itself).
pub fn local_name(raw_name: &str) -> String {
    match raw_name.rsplit_once(':') {
        Some((_, local)) => local.to_string(),
        None => raw_name.to_string(),
    }
}

/// Yield every structural tag in `xml` as a `Tag`, in scan order. The
/// caller owns nesting validation (mirror of `iter_tags`).
pub fn scan_tags(xml: &[u8]) -> Vec<Tag> {
    let mut tags = Vec::new();
    let mut index = 0usize;
    while index < xml.len() {
        let Some(lt) = find_bytes(xml, b"<", index) else {
            break;
        };
        let rest = &xml[lt..];
        let token_end = if rest.starts_with(b"<!--") {
            find_bytes(xml, b"-->", lt + 4).map(|end| end + 3)
        } else if rest.starts_with(b"<![CDATA[") {
            find_bytes(xml, b"]]>", lt + 9).map(|end| end + 3)
        } else if rest.starts_with(b"<?") {
            find_bytes(xml, b"?>", lt + 2).map(|end| end + 2)
        } else {
            scan_angle_token(xml, lt)
        };
        let Some(end) = token_end else {
            break;
        };
        if let Some(tag) = parse_tag(&xml[lt..end], lt, end) {
            tags.push(tag);
        }
        index = end;
    }
    tags
}

/// Build the parent-child element tree over a tag list (nesting-validated).
/// Every element gets byte ranges: `open_end` (just past its start tag),
/// `end` (just past its close tag, or `open_end` for self-closing). Text
/// between `open_end` and the first child tag is the element's own text.
#[derive(Clone, Debug)]
pub struct ElementNode {
    pub name: String,
    pub raw_name: String,
    pub open_start: usize,
    /// Byte offset just past the start tag.
    pub open_end: usize,
    /// Byte offset of the start of the close tag (== `open_end` when
    /// self-closing).
    pub close_start: usize,
    /// Byte offset just past the close tag (== `open_end` when
    /// self-closing).
    pub end: usize,
    pub self_closing: bool,
    pub parent: Option<usize>,
    pub children: Vec<usize>,
}

/// Nesting-validated element forest of one XML part (the `TagCursor`
/// stack discipline, made a tree). Errors on mismatched or unclosed
/// elements, mirroring Python's `ValidationError("malformed ... nesting")`.
pub fn build_tree(tags: &[Tag]) -> Result<Vec<ElementNode>, CoreError> {
    let mut nodes: Vec<ElementNode> = Vec::new();
    let mut stack: Vec<usize> = Vec::new();
    for tag in tags {
        if tag.closing {
            let Some(open) = stack.pop() else {
                return Err(CoreError::Domain(format!(
                    "malformed XML nesting near {} (unmatched close)",
                    tag.raw_name
                )));
            };
            if nodes[open].name != tag.name {
                return Err(CoreError::Domain(format!(
                    "malformed XML nesting near {} (expected close of {}, found {})",
                    tag.raw_name, nodes[open].raw_name, tag.raw_name
                )));
            }
            nodes[open].close_start = tag.start;
            nodes[open].end = tag.end;
            continue;
        }
        let index = nodes.len();
        nodes.push(ElementNode {
            name: tag.name.clone(),
            raw_name: tag.raw_name.clone(),
            open_start: tag.start,
            open_end: tag.end,
            close_start: tag.end,
            end: if tag.self_closing {
                tag.end
            } else {
                usize::MAX
            },
            self_closing: tag.self_closing,
            parent: stack.last().copied(),
            children: Vec::new(),
        });
        if let Some(&parent) = stack.last() {
            nodes[parent].children.push(index);
        }
        if !tag.self_closing {
            stack.push(index);
        }
    }
    if !stack.is_empty() {
        let open = stack.pop().expect("stack non-empty");
        return Err(CoreError::Domain(format!(
            "malformed XML nesting near {} (unclosed element)",
            nodes[open].raw_name
        )));
    }
    Ok(nodes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scans_comments_cdata_pi_and_self_closing() {
        let xml = br#"<?xml version="1.0"?><w:root><!-- c --><w:p/><w:r>a &amp; b<![CDATA[x]]></w:r></w:root>"#;
        let tags = scan_tags(xml);
        let names: Vec<&str> = tags.iter().map(|tag| tag.name.as_str()).collect();
        assert_eq!(names, vec!["root", "p", "r", "r", "root"]);
        assert!(tags[1].self_closing);
        assert!(tags[3].closing);
        assert_eq!(tags[2].bytes(xml), b"<w:r>");
    }

    #[test]
    fn tree_rejects_mismatched_nesting() {
        let xml = b"<a><b></a></b>";
        let tags = scan_tags(xml);
        assert!(build_tree(&tags).is_err());
    }

    #[test]
    fn tree_rejects_unclosed() {
        let xml = b"<a><b></b>";
        let tags = scan_tags(xml);
        assert!(build_tree(&tags).is_err());
    }
}
