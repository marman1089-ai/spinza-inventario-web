(() => {
  const form=document.getElementById('receipt-upload');
  const select=document.getElementById('receipt-expense');
  if(!form||!select)return;
  function fillEmpty(){
    const option=select.selectedOptions[0];if(!option||!option.value)return;
    for(const [name,key] of [['day','day'],['description','description'],['store','store'],['payer','payer']]){
      const input=form.elements.namedItem(name);
      if(input&&!input.value.trim()&&option.dataset[key]) input.value=option.dataset[key];
    }
  }
  select.addEventListener('change',fillEmpty);fillEmpty();
})();
