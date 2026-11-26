//! mechumu-frame: the Virtual Comet's device frame (lib/frame.html, served by the hub) in a window
//! of its own, as Android's emulator has. `mechumu run --frame` starts it.
//!
//! Closing the window saves the device and quits (Quick Boot), like closing Android's emulator.
//! The window closes by itself once the hub is gone, i.e. when QEMU has quit. Files the page saves
//! (screenshots, recordings) go to ~/Downloads.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

use tao::event::{Event, StartCause, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::WindowBuilder;
use wry::WebViewBuilder;

const DEFAULT_HUB: &str = "127.0.0.1:8090";

/// One command to the hub, as the page sends them (POST /cmd); its reply line, if it answered.
fn hub_command(hub: &str, line: &str) -> Option<String> {
    let mut sock = TcpStream::connect(hub).ok()?;
    sock.set_read_timeout(Some(Duration::from_secs(60))).ok()?;
    write!(
        sock,
        "POST /cmd HTTP/1.1\r\nHost: {hub}\r\nX-Mechumu: 1\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{line}",
        line.len()
    )
    .ok()?;
    let mut reply = String::new();
    sock.read_to_string(&mut reply).ok()?;
    reply.split("\r\n\r\n").nth(1).map(|body| body.trim().to_string())
}

fn hub_up(hub: &str) -> bool {
    hub.parse().ok().is_some_and(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok())
}

fn downloads() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join("Downloads")
}

enum Tick {
    HubGone,
}

fn main() -> wry::Result<()> {
    let url = std::env::args().nth(1).unwrap_or_else(|| format!("http://{DEFAULT_HUB}/"));
    let hub = url.trim_start_matches("http://").trim_end_matches('/').to_string();
    let event_loop = EventLoopBuilder::<Tick>::with_user_event().build();
    let window = WindowBuilder::new()
        .with_title("Virtual Comet")
        .with_inner_size(tao::dpi::LogicalSize::new(820.0, 1000.0))
        .build(&event_loop)
        .expect("a window");

    // The page is served by the hub, which starts alongside QEMU: wait for it, then watch for it to go.
    let proxy = event_loop.create_proxy();
    let hub_watch = hub.clone();
    std::thread::spawn(move || {
        while !hub_up(&hub_watch) {
            std::thread::sleep(Duration::from_millis(250));
        }
        while hub_up(&hub_watch) {
            std::thread::sleep(Duration::from_secs(1));
        }
        let _ = proxy.send_event(Tick::HubGone);
    });
    while !hub_up(&hub) {
        std::thread::sleep(Duration::from_millis(250));
    }

    let builder = WebViewBuilder::new()
        .with_url(&url)
        .with_download_started_handler(|_url, path| {
            let name = path.file_name().map(|n| n.to_owned()).unwrap_or_else(|| "comet-download".into());
            *path = downloads().join(name);
            true
        });
    #[cfg(not(target_os = "linux"))]
    let _webview = builder.build(&window)?;
    #[cfg(target_os = "linux")]
    let _webview = {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        builder.build_gtk(window.gtk_window())?
    };

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::NewEvents(StartCause::Init) => {}
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {
                // Save and quit (Quick Boot); if saving fails (still booting, say), quit anyway.
                match hub_command(&hub, "stop") {
                    Some(reply) if reply.starts_with("ok") => {}
                    _ => {
                        hub_command(&hub, "quit");
                    }
                }
                *control_flow = ControlFlow::Exit;
            }
            Event::UserEvent(Tick::HubGone) => *control_flow = ControlFlow::Exit,
            _ => {}
        }
    });
}
