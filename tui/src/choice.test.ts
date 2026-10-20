import {test,expect} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import {TextRenderable} from '@opentui/core';
import {ChoiceList} from './choice.ts';
import {palette} from './theme.ts';

function textNodes(node:any):TextRenderable[]{
  return [...(node instanceof TextRenderable?[node]:[]),...node.getChildren().flatMap(textNodes)];
}

test('общий список: группы, цветные теги, пропуск отключённых пунктов и мышь',async()=>{
  const setup=await createTestRenderer({width:80,height:22,exitOnCtrlC:false});
  const list=new ChoiceList(setup.renderer,'test-choices');list.height=12;setup.renderer.root.add(list);
  let activated=0;list.on('activate',()=>{activated++;});
  try{
    list.options=[
      {name:'first',group:'glm',badges:[{text:'free',tone:'success'}],value:'a'},
      {name:'foreign',group:'glm',disabled:true,value:'disabled'},
      {name:'last',group:'mistral',description:'Подробное описание',value:'b'},
    ];
    list.setSelectedIndex(0);await setup.flush();
    const frame=setup.captureCharFrame();expect(frame).toContain('─ glm · 2 ─');expect(frame).toContain('─ mistral · 1 ─');
    const badge=textNodes(list).flatMap(node=>node.chunks).find(part=>part.text===' free ');
    expect(badge?.fg?.toInts().slice(0,3)).toEqual([125,211,160]);expect(badge?.bg).toBeDefined();
    list.moveDown();expect(list.getSelectedOption()?.value).toBe('b');
    list.moveGroup(-1);expect(list.getSelectedOption()?.value).toBe('a');
    await setup.flush();
    const target=textNodes(list).find(node=>node.chunks.some(part=>part.text.includes('last')))!;
    await setup.mockMouse.click(target.x+3,target.y);expect(list.getSelectedOption()?.value).toBe('b');expect(activated).toBe(1);
    expect(palette.panel).not.toBe(palette.system);
  }finally{await setup.dispose();}
});
