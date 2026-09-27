use axum::{routing::get, Router};

#[tokio::main]
async fn main() {
    let app = Router::new().route("/api/hello", get(|| async { "Hello World" }));

    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".into());
    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}"))
        .await
        .expect("bind failed");

    eprintln!("rust/axum listening on :{port}");
    axum::serve(listener, app).await.expect("serve failed");
}
