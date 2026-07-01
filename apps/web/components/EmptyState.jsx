export default function EmptyState({ title, description, action }) {
  // 空状态组件用于提示用户下一步可执行动作，避免页面没有数据时显得断裂。
  return (
    <section className="empty-state">
      <div className="empty-mark">N</div>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </section>
  );
}
