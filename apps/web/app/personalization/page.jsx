"use client";

import AppShell from "@/components/AppShell";

export default function PersonalizationPage() {
  // 个性化页集中承载创作偏好和自动化策略，后续会写入 User.preferences。
  return (
    <AppShell
      title="个性化"
      subtitle="管理创作偏好与自动化策略，减少用户在生成过程中的决策参与"
      actions={<button className="primary-button">保存设置</button>}
    >
      <section className="grid-2">
        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">创作偏好</div><div className="panel-subtitle">作为后续 NovelBrief 和 Agent 规划的默认约束</div></div>
          </div>
          <div className="panel-body form-grid">
            <div className="field"><label>默认题材</label><input defaultValue="奇幻悬疑、群像、长篇" /></div>
            <div className="field"><label>叙事口味</label><select defaultValue="balanced"><option value="balanced">剧情推进与人物刻画均衡</option><option>强剧情推进</option><option>强人物内心</option></select></div>
            <div className="field"><label>规避风格</label><textarea defaultValue="避免模板化比喻、解释性独白、过度总结、AI 常用转折句。" /></div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">自动化策略</div><div className="panel-subtitle">目标是用户只填写初始需求，系统自动推进生产</div></div>
          </div>
          <div className="panel-body form-grid">
            <div className="field"><label>章节生成策略</label><select><option>自动生成草稿并审校</option><option>只生成大纲</option></select></div>
            <div className="field"><label>记忆同步</label><select><option>每章后自动同步</option><option>人工确认后同步</option></select></div>
            <div className="field"><label>风险处理</label><select><option>中低风险自动修复，高风险提醒</option><option>全部提醒</option></select></div>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div><div className="panel-title">策略执行预览</div><div className="panel-subtitle">后续 Agent 会读取这些偏好作为运行参数</div></div>
        </div>
        <div className="panel-body">
          <table className="table">
            <thead><tr><th>模块</th><th>当前策略</th><th>落地状态</th></tr></thead>
            <tbody>
              <tr><td>Creative Director</td><td>自动规划章节与上下文</td><td><span className="tag yellow">预留</span></td></tr>
              <tr><td>Memory Sync</td><td>每章后抽取结构化记忆</td><td><span className="tag yellow">预留</span></td></tr>
              <tr><td>Style Auditor</td><td>清理 AI 常用句式</td><td><span className="tag yellow">预留</span></td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </AppShell>
  );
}
