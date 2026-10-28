const assert = require('node:assert/strict');
const {parseLog, terminalText, logTokens} = require('./monitor/logs.js');
const first = text => parseLog(text).rows[0];
assert.deepEqual(first('[2026-10-24 15:42:18.021] [warning] ninfer-serve: too large'), {
  raw:'[2026-10-24 15:42:18.021] [warning] ninfer-serve: too large', index:1,
  time:'2026-10-24 15:42:18.021', source:'ninfer-serve', level:'warn', message:'too large'
});
assert.equal(first('2026-10-24 15:42:18.021  ERROR serve | failed').level, 'error');
assert.equal(first('2026-10-24 15:42:18.021  ERROR serve | failed').source, 'serve');
assert.equal(first('slot print_timing: eval time = 4660 ms').source,'slot print_timing');
assert.equal(first('slot print_timing: eval time = 4660 ms').time,'');
assert.equal(first('slot print_timing: eval time = 4660 ms').level,'plain');
assert.equal(first('[strata] thinking: 128 tokens, 29.7 tok/s').source,'strata');
assert.equal(first('[strata] ERROR: model failed').level,'error');
assert.equal(first('CUDA error: out of memory').level,'plain'); // No keyword-based severity guesses.
assert.equal(first('error_count=0 no errors').level,'plain');
assert.equal(first('\x1b[31mERROR: <script>alert(1)</script>\x1b[0m').message,'<script>alert(1)</script>');
assert.equal(terminalText('\x1b]0;title\x07line\x1b[0m\r\n10%\r20%\r\n'),'line\n20%\n');
assert.equal(first('模型加载完成').raw,'模型加载完成');
assert.equal(parseLog('line\n'.repeat(5000)).rows.length,2000);
assert.equal(parseLog('line\n'.repeat(5000)).total,5000);
assert.equal(parseLog('line\n'.repeat(5000)).rows[0].index,3001);
assert.equal(parseLog('').rows.length,0);
assert.equal(parseLog('\n\r\n').rows.length,2);
const aligned='[strata] time = 12 ms\n\n  tensor   type\n\tq_proj   Q4_K\n';
assert.equal(parseLog(aligned).rows.map(row=>row.raw).join('\n')+'\n',aligned);
for(const line of [aligned, '[2026-10-24 15:42:18.021] [warning] module: 29.7 tok/s, 512 MiB, 50%', '<img src=x onerror=alert(1)>', 'error_count=0 no errors']){
  assert.equal(logTokens(line).map(token=>token.text).join(''),line);
}
assert.ok(logTokens('[warning] 29.7 tok/s').some(token=>token.kind==='warn'));
assert.ok(logTokens('[strata] 29.7 tok/s').some(token=>token.kind==='metric'));
assert.ok(!logTokens('error_count=0 no errors').some(token=>token.kind==='error'));
console.log('PASS: three backend formats, severity, missing metadata, ANSI/CR, Unicode and buffer cap');
