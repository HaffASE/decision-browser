use super::{BrowserResult, Cdp, OwnedChrome, failure, number_field, string_field};
use serde_json::{Value, json};
use std::thread;
use std::time::{Duration, Instant};

const OBSERVE: &str = include_str!("../assets/matrix_observation.js");

pub struct MatrixPage {
    cdp: Cdp,
    session: String,
    origin: String,
    world: Option<i64>,
    observed_url: String,
    observed_choices: Vec<Value>,
}

pub fn run_scenario<T>(
    url: &str,
    timeout: Duration,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    let mut chrome = OwnedChrome::launch()?;
    let outcome = (|| {
        let deadline = Instant::now() + timeout;
        let endpoint = chrome.debugging_endpoint(deadline)?;
        let mut cdp = Cdp::connect(&endpoint, deadline)?;
        let context = cdp.call("Target.createBrowserContext", json!({}), None)?;
        let context_id = string_field(&context, "browserContextId")?;
        let target = cdp.call(
            "Target.createTarget",
            json!({"url":"about:blank", "browserContextId":context_id}),
            None,
        )?;
        let target_id = string_field(&target, "targetId")?;
        let attached = cdp.call(
            "Target.attachToTarget",
            json!({"targetId":target_id,"flatten":true}),
            None,
        )?;
        let session = string_field(&attached, "sessionId")?;
        cdp.call("Page.enable", json!({}), Some(&session))?;
        cdp.call("Runtime.enable", json!({}), Some(&session))?;
        let parsed = reqwest::Url::parse(url)?;
        if !(parsed.scheme() == "http" && parsed.host_str() == Some("127.0.0.1")
            || url == "https://www.python.org/")
        {
            return Err(failure("matrix URL is outside the fixture boundary"));
        }
        let mut page = MatrixPage {
            cdp,
            session,
            origin: parsed.origin().ascii_serialization(),
            world: None,
            observed_url: String::new(),
            observed_choices: Vec::new(),
        };
        let navigation = page
            .cdp
            .call("Page.navigate", json!({"url":url}), Some(&page.session))?;
        if navigation.get("errorText").is_some() {
            return Err(failure("initial navigation failed"));
        }
        page.ready()?;
        let result = run(&mut page);
        let _ = page.cdp.call("Browser.close", json!({}), None);
        result
    })();
    let cleanup = chrome.finish();
    match (outcome, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) | (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(cleanup)) => Err(failure(format!("{error}; cleanup failed: {cleanup}"))),
    }
}

impl MatrixPage {
    fn ready(&mut self) -> BrowserResult<()> {
        loop {
            if self.cdp.evaluate(
                "document.readyState === 'complete' && location.href !== 'about:blank'",
                &self.session,
            )? == true
            {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    fn enter_world(&mut self) -> BrowserResult<()> {
        let tree = self
            .cdp
            .call("Page.getFrameTree", json!({}), Some(&self.session))?;
        let frame = tree
            .pointer("/frameTree/frame/id")
            .and_then(Value::as_str)
            .ok_or_else(|| failure("main frame missing"))?;
        let created = self.cdp.call(
            "Page.createIsolatedWorld",
            json!({"frameId":frame,"worldName":"decision-browser-matrix"}),
            Some(&self.session),
        )?;
        self.world = created["executionContextId"].as_i64();
        if self.world.is_none() {
            return Err(failure("isolated world missing"));
        }
        Ok(())
    }

    fn eval(&mut self, expression: &str) -> BrowserResult<Value> {
        let context = self
            .world
            .ok_or_else(|| failure("snapshot required before action"))?;
        let result = self.cdp.call(
            "Runtime.evaluate",
            json!({"expression":expression,"contextId":context,"returnByValue":true}),
            Some(&self.session),
        )?;
        if result.get("exceptionDetails").is_some() {
            return Err(failure("matrix evaluation failed"));
        }
        result
            .pointer("/result/value")
            .cloned()
            .ok_or_else(|| failure("matrix evaluation returned no value"))
    }

    pub fn snapshot(&mut self) -> BrowserResult<Value> {
        self.ready()?;
        self.enter_world()?;
        let state = self.eval(OBSERVE)?;
        let url = string_field(&state, "url")?;
        self.check_url(&url)?;
        self.observed_url = url;
        self.observed_choices = state["choices"]
            .as_array()
            .ok_or_else(|| failure("snapshot choices missing"))?
            .clone();
        Ok(state)
    }

    fn check_url(&self, url: &str) -> BrowserResult<()> {
        let parsed = reqwest::Url::parse(url)?;
        if parsed.origin().ascii_serialization() != self.origin {
            return Err(failure("navigation escaped matrix origin"));
        }
        if self.origin == "https://www.python.org" && !["/", "/about/"].contains(&parsed.path()) {
            return Err(failure("public navigation escaped About scope"));
        }
        Ok(())
    }

    fn element_expression(&self, target: &str) -> BrowserResult<String> {
        Ok(format!(
            "globalThis.__matrixNodes?.get({})",
            serde_json::to_string(target)?
        ))
    }

    fn geometry(&mut self, target: &str) -> BrowserResult<Value> {
        let element = self.element_expression(target)?;
        let expected_url = serde_json::to_string(&self.observed_url)?;
        self.eval(&format!("(() => {{if(location.href!=={expected_url})return null; const e={element}; if(!e||!e.isConnected||e.disabled)return null; const r=e.getBoundingClientRect(); const x=r.x+r.width/2,y=r.y+r.height/2; const h=document.elementFromPoint(x,y); if(r.width<=0||r.height<=0||x<0||y<0||x>=innerWidth||y>=innerHeight||!(h===e||e.contains(h)))return null; return {{x,y,label:globalThis.__matrixLabel(e),value:e.value,tag:e.tagName,type:e.type}}; }})()"))
    }

    fn focus(&mut self, target: &str) -> BrowserResult<()> {
        let context = self.world.ok_or_else(|| failure("snapshot required"))?;
        let result = self.cdp.call(
            "Runtime.evaluate",
            json!({"expression":self.element_expression(target)?,"contextId":context}),
            Some(&self.session),
        )?;
        let object = result
            .pointer("/result/objectId")
            .and_then(Value::as_str)
            .ok_or_else(|| failure("stale focus target"))?;
        let node = self.cdp.call(
            "DOM.describeNode",
            json!({"objectId":object}),
            Some(&self.session),
        )?;
        let backend = node
            .pointer("/node/backendNodeId")
            .and_then(Value::as_u64)
            .ok_or_else(|| failure("focus node missing"))?;
        self.cdp.call(
            "DOM.focus",
            json!({"backendNodeId":backend}),
            Some(&self.session),
        )?;
        self.cdp.call(
            "Runtime.releaseObject",
            json!({"objectId":object}),
            Some(&self.session),
        )?;
        Ok(())
    }

    pub fn act(&mut self, operation: &str, target: &str, value: Option<&str>) -> BrowserResult<()> {
        match operation {
            "wait" => thread::sleep(Duration::from_millis(300)),
            "scroll" => {
                let delta = if value == Some("up") || target == "up" {
                    -500
                } else {
                    500
                };
                self.cdp.call(
                    "Input.dispatchMouseEvent",
                    json!({"type":"mouseWheel","x":450,"y":300,"deltaX":0,"deltaY":delta}),
                    Some(&self.session),
                )?;
            }
            "click" | "submit" | "fill" | "select" | "enter" => {
                let geometry = self.geometry(target)?;
                if geometry.is_null() {
                    return Err(failure("target became stale, obscured or offscreen"));
                }
                let choice = self
                    .observed_choices
                    .iter()
                    .find(|choice| choice["id"] == target)
                    .ok_or_else(|| failure("target was not offered in the snapshot"))?;
                let offered = choice["operation"].as_str();
                let compatible = offered == Some(operation)
                    || operation == "submit" && offered == Some("click")
                    || operation == "enter" && offered == Some("fill");
                if !compatible || choice["label"] != geometry["label"] {
                    return Err(failure("target semantics changed after snapshot"));
                }
                let element = self.element_expression(target)?;
                let href = self.eval(&format!(
                    "{element}.tagName === 'A' ? {element}.href : null"
                ))?;
                if let Some(href) = href.as_str() {
                    self.check_url(href)?;
                }
                match operation {
                    "fill" => {
                        if geometry["tag"] != "INPUT" && geometry["tag"] != "TEXTAREA" {
                            return Err(failure("fill target is not editable"));
                        }
                        self.focus(target)?;
                        self.cdp.call("Input.dispatchKeyEvent", json!({"type":"keyDown","key":"a","code":"KeyA","modifiers":if cfg!(target_os="macos"){4}else{2},"commands":["selectAll"]}), Some(&self.session))?;
                        self.cdp.call(
                            "Input.dispatchKeyEvent",
                            json!({"type":"keyUp","key":"a","code":"KeyA"}),
                            Some(&self.session),
                        )?;
                        self.cdp.call(
                            "Input.insertText",
                            json!({"text":value.ok_or_else(|| failure("fill value missing"))?}),
                            Some(&self.session),
                        )?;
                    }
                    "select" => {
                        if geometry["tag"] != "SELECT" {
                            return Err(failure("select target is not a select"));
                        }
                        let element = self.element_expression(target)?;
                        let desired = serde_json::to_string(
                            value.ok_or_else(|| failure("select value missing"))?,
                        )?;
                        if self.eval(&format!("(() => {{const e={element};const v={desired};const matches=Array.from(e.options).filter(o=>!o.disabled&&(o.value===v||o.textContent.trim()===v));if(matches.length!==1)return false;Reflect.apply(Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set,e,[matches[0].value]);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()"))? != true {
                            return Err(failure("select option is absent or ambiguous"));
                        }
                    }
                    "enter" => {
                        self.focus(target)?;
                        for kind in ["keyDown", "keyUp"] {
                            self.cdp.call("Input.dispatchKeyEvent", json!({"type":kind,"key":"Enter","code":"Enter","windowsVirtualKeyCode":13,"nativeVirtualKeyCode":13}), Some(&self.session))?;
                        }
                    }
                    _ => {
                        let x = number_field(&geometry, "x")?;
                        let y = number_field(&geometry, "y")?;
                        self.cdp.call(
                            "Input.dispatchMouseEvent",
                            json!({"type":"mouseMoved","x":x,"y":y}),
                            Some(&self.session),
                        )?;
                        if self.geometry(target)? != geometry {
                            return Err(failure("target changed after hover"));
                        }
                        for (kind, buttons) in [("mousePressed", 1), ("mouseReleased", 0)] {
                            self.cdp.call("Input.dispatchMouseEvent", json!({"type":kind,"x":x,"y":y,"button":"left","buttons":buttons,"clickCount":1}), Some(&self.session))?;
                        }
                    }
                }
            }
            _ => return Err(failure("unsupported matrix operation")),
        }
        thread::sleep(Duration::from_millis(200));
        Ok(())
    }

    pub fn read_check(&mut self, check: &Value) -> BrowserResult<bool> {
        self.ready()?;
        self.enter_world()?;
        let kind = check["type"]
            .as_str()
            .ok_or_else(|| failure("check type missing"))?;
        let observed = if kind == "url_equals" {
            self.eval("location.href")?
        } else {
            let selector = serde_json::to_string(
                check["target"]
                    .as_str()
                    .ok_or_else(|| failure("check target missing"))?,
            )?;
            match kind {
                "text_equals" => self.eval(&format!(
                    "document.querySelector({selector})?.textContent?.trim() ?? null"
                ))?,
                "value_equals" => self.eval(&format!(
                    "document.querySelector({selector})?.value ?? null"
                ))?,
                _ => return Err(failure("unsupported matrix check")),
            }
        };
        Ok(observed == check["value"])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::browser::CounterServer;

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn matrix_fixture_labels_and_native_form_actions() -> BrowserResult<()> {
        for case in ["search", "hotel", "profile_form"] {
            let fixture = crate::matrix_fixtures::start(case)?;
            run_scenario(&fixture.url, Duration::from_secs(15), |page| {
                let values = fixture
                    .values
                    .as_object()
                    .ok_or_else(|| failure("values missing"))?;
                for (label, value) in values {
                    let state = page.snapshot()?;
                    let choice = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["operation"] == "fill" && choice["label"] == *label)
                        .ok_or_else(|| failure(format!("{case}: no fill choice for {label}")))?;
                    page.act("fill", &string_field(choice, "id")?, value.as_str())?;
                    let after = page.snapshot()?;
                    assert!(
                        after["choices"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .any(|choice| choice["label"] == *label && choice["current"] == *value)
                    );
                }
                if case == "profile_form" {
                    let state = page.snapshot()?;
                    let select = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["label"] == "Delivery")
                        .unwrap();
                    page.act("select", &string_field(select, "id")?, Some("Express"))?;
                }
                if case != "hotel" {
                    let state = page.snapshot()?;
                    let label = if case == "search" {
                        "Search"
                    } else {
                        "Confirm profile"
                    };
                    let button = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["label"] == label)
                        .unwrap();
                    page.act("click", &string_field(button, "id")?, None)?;
                    let deadline = Instant::now() + Duration::from_secs(2);
                    loop {
                        let checks = fixture
                            .checks
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|check| page.read_check(check))
                            .collect::<BrowserResult<Vec<_>>>()?;
                        if checks.into_iter().all(|passed| passed) && fixture.verify()? {
                            break;
                        }
                        assert!(
                            Instant::now() < deadline,
                            "{case}: fixture did not acknowledge input before deadline"
                        );
                        thread::sleep(Duration::from_millis(20));
                    }
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn matrix_snapshot_input_and_checks_use_native_cdp() -> BrowserResult<()> {
        let fixture = CounterServer::start()?;
        run_scenario(&fixture.url, Duration::from_secs(15), |page| {
            let state = page.snapshot()?;
            let button = state["choices"]
                .as_array()
                .and_then(|choices| choices.iter().find(|choice| choice["label"] == "inc"))
                .ok_or_else(|| failure("counter button missing from snapshot"))?;
            let target = string_field(button, "id")?;
            assert!(page.act("click", "absent", None).is_err());
            assert!(page.read_check(&json!({"type":"text_equals","target":"#count","value":"0"}))?);
            page.snapshot()?;
            page.act("click", &target, None)?;
            assert!(page.read_check(&json!({"type":"text_equals","target":"#count","value":"1"}))?);
            assert!(page.read_check(&json!({"type":"url_equals","value":fixture.url}))?);
            assert!(page.check_url("https://example.org/").is_err());
            Ok(())
        })
    }
}
