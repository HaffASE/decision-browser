//! Classify observable browser effects from the target and its exact destination.

use reqwest::Url;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Classification {
    Ordinary,
    Consequential,
    Unresolved,
}

pub fn validate_url(value: &str) -> Result<Url, &'static str> {
    let (_, rest) = value.split_once("://").ok_or("invalid_destination")?;
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty()
        || authority.contains('@')
        || value.contains('\\')
        || value.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err("invalid_destination");
    }
    let url = Url::parse(value).map_err(|_| "invalid_destination")?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || url.port() == Some(0)
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err("invalid_destination");
    }
    Ok(url)
}

pub fn is_loopback(url: &Url) -> bool {
    matches!(url.host_str(), Some("127.0.0.1" | "[::1]" | "localhost"))
}

pub fn classify(
    destination_url: &str,
    signals: &[String],
    signal_conflict: bool,
) -> Classification {
    let Ok(url) = validate_url(destination_url) else {
        return Classification::Unresolved;
    };
    for part in [Some(url.path()), url.query()].into_iter().flatten() {
        if words(&decode_percent(part))
            .iter()
            .any(|word| consequential(word))
        {
            return Classification::Consequential;
        }
    }
    for signal in signals {
        let words = words(signal);
        if matches!(words.as_slice(), [verb, order] if (verb == "sort" || verb == "apply") && order == "order")
        {
            continue;
        }
        if words.iter().any(|word| consequential(word)) {
            return Classification::Consequential;
        }
    }
    if signal_conflict {
        Classification::Unresolved
    } else {
        Classification::Ordinary
    }
}

fn decode_percent(value: &str) -> String {
    let mut output = Vec::with_capacity(value.len());
    let mut input = value.bytes().peekable();
    while let Some(byte) = input.next() {
        if byte == b'%' {
            let mut suffix = input.clone();
            let decoded = suffix
                .next()
                .and_then(|high| char::from(high).to_digit(16))
                .zip(suffix.next().and_then(|low| char::from(low).to_digit(16)));
            if let Some((high, low)) = decoded {
                output.push(((high << 4) | low) as u8);
                input = suffix;
                continue;
            }
        }
        output.push(byte);
    }
    String::from_utf8_lossy(&output).into_owned()
}

fn words(value: &str) -> Vec<String> {
    let mut result = Vec::new();
    let mut current = String::new();
    let mut input = value.chars().peekable();
    let mut previous = None::<char>;
    while let Some(ch) = input.next() {
        let camel_boundary = ch.is_ascii_uppercase()
            && previous.is_some_and(|prev| {
                prev.is_ascii_lowercase()
                    || prev.is_ascii_digit()
                    || (prev.is_ascii_uppercase()
                        && input.peek().is_some_and(char::is_ascii_lowercase))
            });
        if (!ch.is_alphanumeric() || camel_boundary) && !current.is_empty() {
            result.push(std::mem::take(&mut current));
        }
        if ch.is_alphanumeric() {
            current.extend(ch.to_lowercase());
        }
        previous = Some(ch);
    }
    if !current.is_empty() {
        result.push(current);
    }
    result
}

fn consequential(word: &str) -> bool {
    matches!(
        word,
        "checkout"
            | "checkouts"
            | "purchase"
            | "purchases"
            | "buy"
            | "pay"
            | "payment"
            | "payments"
            | "send"
            | "publish"
            | "delete"
            | "remove"
            | "transfer"
            | "transfers"
            | "booking"
            | "bookings"
            | "reserve"
            | "order"
            | "orders"
            | "оформить"
            | "оформление"
            | "купить"
            | "перевести"
            | "заказать"
    ) || [
        "покупк",
        "оплат",
        "платеж",
        "платёж",
        "отправ",
        "опубликов",
        "удал",
        "убра",
        "перевод",
        "бронирован",
        "заброниров",
        "резерв",
        "зарезервир",
        "заказ",
    ]
    .iter()
    .any(|prefix| word.starts_with(prefix))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_ambiguous_or_credentialed_urls() {
        for url in [
            "https:example.org",
            "https:///example.org",
            "https://@example.org/",
            "https://user:pw@example.org/",
            "http://example.org:0/",
            "http://[::1/",
            "http://example.org\\other",
            "https://example.org/a\tb",
            "file:///var/tmp",
        ] {
            assert!(validate_url(url).is_err(), "{url}");
        }
        assert!(is_loopback(&validate_url("http://[::1]:9080/").unwrap()));
        assert!(validate_url("https://example.org/a%20b").is_ok());
    }

    #[test]
    fn classifies_target_identifiers_and_decoded_destination_routes() {
        for (url, signal) in [
            ("https://example.org/a", "confirmPayment"),
            ("https://example.org/a", "APIDeleteButton"),
            ("https://example.org/a", "отправить_форму"),
            ("https://example.org/api/%64elete", "Continue"),
            ("https://example.org/api?next=place_order", "Continue"),
            (
                "https://example.org/export?method=%D0%BE%D0%BF%D0%BB%D0%B0%D1%82%D0%B0",
                "Continue",
            ),
        ] {
            assert_eq!(
                classify(url, &[signal.to_owned()], true),
                Classification::Consequential
            );
        }
    }

    #[test]
    fn preserves_ordering_label_exception_and_closed_conflicts() {
        let sorting = ["Apply order".to_owned()];
        assert_eq!(
            classify("https://orders.example.org/sort", &sorting, false),
            Classification::Ordinary
        );
        assert_eq!(
            classify("https://orders.example.org/sort", &sorting, true),
            Classification::Unresolved
        );
        assert_eq!(
            classify("https://orders.example.org/applyOrder", &sorting, false),
            Classification::Consequential
        );
        assert_eq!(
            classify(
                "https://orders.example.org/sort",
                &["Sort order".to_owned(), "order".to_owned()],
                false
            ),
            Classification::Consequential
        );
    }
}
