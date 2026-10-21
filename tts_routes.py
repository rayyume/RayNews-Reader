"""Authenticated TTS APIs; browser requests never carry provider credentials."""
import hashlib
import tempfile
from itertools import chain
from pathlib import Path

from flask import Blueprint, Response, g, jsonify, request, send_file

from ai_access import access
from auth import require_role
from tts_cache import cache
from tts_service import TTSError, configured, get_config, listening_text, public_config, reading_content, save_config, synthesize


def register_tts_routes(app, load_article, load_result, namespace):
    bp = Blueprint("tts", __name__)

    @bp.after_request
    def private(response):
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @bp.errorhandler(TTSError)
    def failed(error):
        payload = {"code": error.code, "error": str(error)}
        if getattr(g, "user_role", "") == "admin" and error.admin_detail:
            payload["detail"] = error.admin_detail
        return jsonify(payload), error.status

    @bp.errorhandler(OSError)
    def storage_failed(error):
        return jsonify({"code": "TTS_STORAGE_ERROR", "error": "语音临时存储不可用，请稍后重试"}), 503

    def permitted():
        current = access(g.user_id)
        if not current["permissions"]["tts"]:
            raise TTSError("AI_PERMISSION_DENIED", "暂无语音阅读权限，请联系管理员", 403)
        if not configured():
            raise TTSError("TTS_UNAVAILABLE", "语音服务暂未开放", 409)
        if not current["view_tts"]:
            raise TTSError("TTS_DISABLED", "请先在 AI 功能中开启语音阅读", 403)
        return current

    def prepare(article_id, current, with_summary=None):
        article = load_article(article_id)
        if not article:
            raise TTSError("TTS_ARTICLE_NOT_FOUND", "文章不存在或已删除", 404)
        if with_summary and not current["view_summary"]:
            raise TTSError("AI_PERMISSION_DENIED", "摘要访问权限已变更，请重新生成语音", 403)
        result = load_result(article_id, current)
        article, selection, aliases = reading_content(article, result)
        summary = result.get("summary") if current["view_summary"] and with_summary is not False else ""
        text, has_summary = listening_text(article.get("title", ""), article.get("body_html", ""),
                                           summary or "", title_aliases=aliases)
        if has_summary:
            selection["required_preferences"] = sorted({*selection["required_preferences"], "view_summary"})
        return article, text, has_summary, selection

    def checked_job(job_id):
        current = permitted()
        job = cache.get(job_id)
        if any(not current.get(preference) for preference in job.get("selection", {}).get("required_preferences", [])):
            raise TTSError("AI_PERMISSION_DENIED", "朗读内容的访问权限或显示设置已变更，请重新生成语音", 403)
        _, text, _, selection = prepare(job["article_id"], current, job["has_summary"])
        if (job["revision"] != get_config()["revision"] or selection != job.get("selection") or
                hashlib.sha256(text.encode()).hexdigest() != job["text_hash"]):
            raise TTSError("TTS_EXPIRED", "文章、译文、显示标题或语音配置已更新，请重新生成", 410)
        return job

    def job_response(job):
        result = {key: job[key] for key in ("id", "article_id", "status", "has_summary")}
        result.update(job.get("selection", {}))
        if job["status"] != "failed":
            result["stream_url"] = "/ai/tts/stream/" + job["id"]
        if job["status"] == "ready":
            result["audio_url"] = "/ai/tts/audio/" + job["id"]
        if job["status"] == "failed":
            result.update(error=job["error"], code=job["code"])
            if g.user_role == "admin" and job.get("admin_detail"):
                result["detail"] = job["admin_detail"]
        return result

    @bp.route("/admin/tts-config", methods=["GET", "PUT"])
    @require_role("admin")
    def config():
        if request.method == "GET":
            return jsonify(public_config(get_config()))
        try:
            return jsonify(public_config(save_config(request.get_json(silent=True))))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.route("/admin/tts-test", methods=["POST"])
    @require_role("admin")
    def test():
        settings = {**get_config(), "enabled": True}
        # Test uses saved credentials but can run before enabling the service.
        with tempfile.TemporaryDirectory(prefix="raynews-tts-test-") as directory:
            target = Path(directory) / "test.mp3"
            synthesize(settings, "你好，欢迎使用 RayNews。语音阅读服务连接正常。", target, max_bytes=2 * 1024 * 1024)
            return Response(target.read_bytes(), mimetype="audio/mpeg")

    @bp.route("/ai/tts/<int:article_id>", methods=["POST"])
    @require_role("user", "admin")
    def generate(article_id):
        current = permitted()
        article, text, has_summary, selection = prepare(article_id, current)
        settings = get_config()
        # All text is read from the canonical server store; client-supplied text,
        # URLs, credentials and arbitrary model parameters are never forwarded.
        job = cache.request(article_id, g.user_id, settings, text, has_summary,
                            namespace=namespace() + ":" + str(article_id),
                            valid=lambda: load_article(article_id) is not None, selection=selection)
        result = job_response(job)
        result["article"] = {key: article.get(key, "") for key in (
            "id", "title", "source", "display_source", "display_source_key", "source_curated", "source_labels_read_at",
            "thumb", "date", "time")}
        result["partial_content"] = article.get("has_full_content") == 0
        return jsonify(result), 200 if job["status"] == "ready" else 202

    @bp.route("/ai/tts/jobs/<job_id>", methods=["GET"])
    @require_role("user", "admin")
    def job_status(job_id):
        return jsonify(job_response(checked_job(job_id)))

    @bp.route("/ai/tts/audio/<job_id>", methods=["GET"])
    @require_role("user", "admin")
    def audio(job_id):
        checked_job(job_id)
        path = cache.acquire(job_id)
        try:
            response = send_file(path, mimetype="audio/mpeg", conditional=True, download_name="news.mp3")
            response.direct_passthrough = False
            response.call_on_close(lambda: cache.release(job_id))
            return response
        except Exception:
            cache.release(job_id)
            raise

    @bp.route("/ai/tts/stream/<job_id>", methods=["GET"])
    @require_role("user", "admin")
    def stream_audio(job_id):
        checked_job(job_id)
        chunks = cache.stream(job_id)
        # Report failures before the first audio bytes as ordinary JSON errors.
        # Later failures interrupt the transfer; clients check job status for detail.
        first = next(chunks, b"")
        response = Response(chain((first,), chunks), mimetype="audio/mpeg",
                            headers={"X-Accel-Buffering": "no", "Content-Disposition": "inline; filename=news.mp3"})
        response.call_on_close(chunks.close)
        return response

    app.register_blueprint(bp)
