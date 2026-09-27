var builder = WebApplication.CreateSlimBuilder(args);
builder.Logging.SetMinimumLevel(LogLevel.Warning);

var app = builder.Build();
app.MapGet("/api/hello", () => Results.Text("Hello World", "text/plain; charset=utf-8"));
app.Run();
