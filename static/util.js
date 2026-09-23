// Custom Chart.js date adapter using native Date APIs
Chart._adapters._date.override({
  formats() {
    return {
      datetime: 'MMM d, yyyy, HH:mm:ss',
      millisecond: 'HH:mm:ss.SSS',
      second: 'HH:mm:ss',
      minute: 'HH:mm',
      hour: 'HH:mm',
      day: 'MMM d',
      week: 'MMM d',
      month: 'MMM yyyy',
      quarter: 'MMM yyyy',
      year: 'yyyy'
    };
  },

  // Handles: Unix timestamps (ms), Date objects, ISO strings
  parse(value) {
    if (value == null) return null;
    if (typeof value === 'number') return value;
    if (value instanceof Date) return value.getTime();
    return Date.parse(value); // ISO format
  },

  format(ts, fmt) {
    const d = new Date(ts);
    const pad = n => String(n).padStart(2, '0');
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    const tokens = {
      yyyy: d.getFullYear(),
      MMM: months[d.getMonth()],
      d: d.getDate(),
      HH: pad(d.getHours()),
      mm: pad(d.getMinutes()),
      ss: pad(d.getSeconds()),
      SSS: String(d.getMilliseconds()).padStart(3, '0')
    };

    return fmt.replace(/yyyy|MMM|d|HH|mm|ss|SSS/g, m => tokens[m]);
  },

  add(ts, amount, unit) {
    const d = new Date(ts);
    switch (unit) {
      case 'millisecond': return ts + amount;
      case 'second': return ts + amount * 1000;
      case 'minute': return ts + amount * 60000;
      case 'hour': return ts + amount * 3600000;
      case 'day': d.setDate(d.getDate() + amount); break;
      case 'week': d.setDate(d.getDate() + amount * 7); break;
      case 'month': d.setMonth(d.getMonth() + amount); break;
      case 'quarter': d.setMonth(d.getMonth() + amount * 3); break;
      case 'year': d.setFullYear(d.getFullYear() + amount); break;
    }
    return d.getTime();
  },

  diff(a, b, unit) {
    const ms = a - b;
    switch (unit) {
      case 'millisecond': return ms;
      case 'second': return Math.floor(ms / 1000);
      case 'minute': return Math.floor(ms / 60000);
      case 'hour': return Math.floor(ms / 3600000);
      case 'day': return Math.floor(ms / 86400000);
      case 'week': return Math.floor(ms / 604800000);
      case 'month': return (new Date(a).getFullYear() - new Date(b).getFullYear()) * 12
                         + new Date(a).getMonth() - new Date(b).getMonth();
      case 'year': return new Date(a).getFullYear() - new Date(b).getFullYear();
      default: return ms;
    }
  },

  startOf(ts, unit) {
    const d = new Date(ts);
    switch (unit) {
      case 'second': d.setMilliseconds(0); break;
      case 'minute': d.setSeconds(0, 0); break;
      case 'hour': d.setMinutes(0, 0, 0); break;
      case 'day': d.setHours(0, 0, 0, 0); break;
      case 'week': d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - d.getDay()); break;
      case 'month': d.setHours(0, 0, 0, 0); d.setDate(1); break;
      case 'quarter': d.setHours(0, 0, 0, 0); d.setMonth(Math.floor(d.getMonth() / 3) * 3, 1); break;
      case 'year': d.setHours(0, 0, 0, 0); d.setMonth(0, 1); break;
    }
    return d.getTime();
  },

  endOf(ts, unit) {
    const d = new Date(ts);
    switch (unit) {
      case 'second': d.setMilliseconds(999); break;
      case 'minute': d.setSeconds(59, 999); break;
      case 'hour': d.setMinutes(59, 59, 999); break;
      case 'day': d.setHours(23, 59, 59, 999); break;
      case 'week': d.setDate(d.getDate() + (6 - d.getDay())); d.setHours(23, 59, 59, 999); break;
      case 'month': d.setMonth(d.getMonth() + 1, 0); d.setHours(23, 59, 59, 999); break;
      case 'year': d.setMonth(11, 31); d.setHours(23, 59, 59, 999); break;
    }
    return d.getTime();
  }
});
