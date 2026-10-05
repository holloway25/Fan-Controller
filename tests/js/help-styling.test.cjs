const assert=require('assert');
const H=require('./harness.cjs');
module.exports={
  async test_off_draw_help_uses_shared_block_hint_style(){
    const page=await H.open();
    const hint=page.document.querySelector('label[for="watts_off_max"] .hint');
    assert(hint,'OFF draw explanation must use the shared hint class');
    const style=page.window.getComputedStyle(hint);
    assert.strictEqual(style.display,'block');
    assert.strictEqual(style.fontSize,'12.5px');
    assert.strictEqual(style.lineHeight,'1.45');
  },
  async test_standalone_email_help_is_smaller_muted_and_separated(){
    const page=await H.open();
    const hints=[...page.document.querySelectorAll('p.hint')];
    assert.strictEqual(hints.length,2);
    const rowHint=page.document.querySelector('.row .hint');
    const reference=page.window.getComputedStyle(rowHint);
    for(const hint of hints){
      const style=page.window.getComputedStyle(hint);
      assert.strictEqual(style.fontSize,reference.fontSize);
      assert.strictEqual(style.color,reference.color);
      assert.strictEqual(style.lineHeight,reference.lineHeight);
      assert.strictEqual(style.marginTop,'10px');
    }
  },
};
