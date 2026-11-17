# TavernNovelMobile app

## 发版（GitHub Actions）

推 `master` → `.github/workflows/build-release.yml` 构建 arm64 APK → 上传到本仓库
的 `latest` Release（tag 与资产名固定，App 端只认这一个入口）。

- **串行化**：workflow 级 `concurrency: release-latest`（`cancel-in-progress: false`）。
  不取消是故意的——`gh release upload --clobber` 是「删旧资产 → 传新资产」两段，
  中途取消可能留下一个没有 APK 的 latest。
- **版本单调**：Release 步骤先跑 `python tools/release_guard.py decide`，云端
  versionCode 更大就跳过上传（防「重跑旧提交」把 latest 降级）。
- **说明格式（冻结）**：前两行是机器可读元数据，第三行起才是给人看的
  ```
  <!-- appupdate versionCode=53 versionName=0.1.53 -->
  <!-- apkupdate apkSha256=<64 位小写 hex> -->
  ```
  首行字段顺序与结尾**不能改**：0.1.52 及更早的客户端用严格正则整行匹配，
  多一个属性就让老用户永远收不到更新。新字段只能加在第二行注释里。
- **完整性**：App 内下载 → 复用缓存 → 安装前三处都校验 `apkSha256`，不符即删包、
  不唤起安装器。浏览器通路只展示官方地址 + SHA-256，App 拿不到文件，**不宣称已校验**。

测试（本地，不联网）：

```bash
python -m pytest tests/test_mobile_update.py tests/test_release_workflow.py -q
```

## CI 基线（2026-11-17）

推 `master` 后 **先测后发**：`test` job 跑 update 专项（`tests/test_mobile_update.py`）
与手机测试（`tests/test_release_workflow.py`），`build` job 用 `needs: test` 挡在后面；
两个 job 都只装 `constraints.txt` 锁定的已实测版本（CI 工具链）；
`flet build apk` 内部的 Android 独立环境不能套 `PIP_CONSTRAINT`（会与
Python 3.14 / `--only-binary` 解析冲突），改由 `pyproject.toml` 的精确
`==` pin 锁定。运行时依赖在 `requirements.txt`、开发/测试依赖在
`requirements-dev.txt`，两者分开。

- 不加 `-k` / `--ignore`；`--strict-markers --runxfail` + `tools/ci/check_pytest_results.py`
  逐条核查 JUnit，任何 skipped / xfail / failure 都会判红；失败时上传测试日志。
- `tests/test_app.py` 是 Flet 渲染级集成测试，`flet test` 会自起原生宿主并按
  pyproject 重新解析未锁定依赖，与锁文件策略冲突；workflow 里有显式范围说明，
  由本机 UI 验收覆盖，手机控件树回归在桌面仓 CI 执行。
- `concurrency: cancel-in-progress: false` 是发版红线，不要改回 true。

桌面仓对应基线见 `docs/模块总览/CI基线模块总览.md`。

## Run the app

### uv

Run as a desktop app:

```bash
uv run flet run
```

Run as a web app:

```bash
uv run flet run --web
```

For more details on running the app, refer to the [Getting Started Guide](https://flet.dev/docs/).

## Build the app

### Android

```bash
flet build apk -v
```

For more details on building and signing `.apk` or `.aab`, refer to the [Android Packaging Guide](https://flet.dev/docs/publish/android/).

### iOS

```bash
flet build ipa -v
```

For more details on building and signing `.ipa`, refer to the [iOS Packaging Guide](https://flet.dev/docs/publish/ios/).

### macOS

```bash
flet build macos -v
```

For more details on building macOS package, refer to the [macOS Packaging Guide](https://flet.dev/docs/publish/macos/).

### Linux

```bash
flet build linux -v
```

For more details on building Linux package, refer to the [Linux Packaging Guide](https://flet.dev/docs/publish/linux/).

### Windows

```bash
flet build windows -v
```

For more details on building Windows package, refer to the [Windows Packaging Guide](https://flet.dev/docs/publish/windows/).

### Web

```bash
flet build web -v
```

For more details on building Web app, refer to the [Web Packaging Guide](https://flet.dev/docs/publish/web/).
