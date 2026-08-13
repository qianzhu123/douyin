export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') return '-';
  const total = Math.max(0, Math.round(Number(seconds)));
  if (!Number.isFinite(total)) return '-';

  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainingSeconds = total % 60;
  const parts = [];

  if (hours > 0) parts.push(`${hours}小时`);
  if (minutes > 0) parts.push(`${minutes}分钟`);
  if (remainingSeconds > 0 || parts.length === 0) parts.push(`${remainingSeconds}秒`);

  return parts.join('');
}
