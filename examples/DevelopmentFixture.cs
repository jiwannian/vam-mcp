using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

namespace VaMMcpExamples
{
    public class DevelopmentFixture : MVRScript
    {
        private JSONStorableFloat count;
        private JSONStorableFloat level;
        private JSONStorableString input;
        private JSONStorableString chosenFile;
        private JSONStorableString accepted;
        private JSONStorableBool enabledOption;
        public override void Init()
        {
            count = new JSONStorableFloat("count", 0, 0, 10000); RegisterFloat(count);
            level = new JSONStorableFloat("level", .5f, 0, 1); RegisterFloat(level); CreateSlider(level);
            enabledOption = new JSONStorableBool("option", false); RegisterBool(enabledOption); CreateToggle(enabledOption);
            input = new JSONStorableString("input", ""); RegisterString(input);
            chosenFile = new JSONStorableString("chosenFile", ""); RegisterString(chosenFile);
            accepted = new JSONStorableString("submitted", ""); RegisterString(accepted);
            JSONStorableString version = new JSONStorableString("version", "fixture-v1"); RegisterString(version);
            JSONStorableStringChooser chooser = new JSONStorableStringChooser("choice", new List<string> { "alpha", "beta", "gamma" }, "alpha", "Choice");
            RegisterStringChooser(chooser); CreatePopup(chooser);
            RegisterAction(new JSONStorableAction("Increment", Increment));
            FixtureButton("Increment", "Increment", Increment);
            UIDynamicButton disabled = FixtureButton("Disabled", "Disabled", null); disabled.button.interactable = false;
            FixtureButton("Open confirmation", "Confirm", delegate { SuperController.AlertUser("Fixture confirmation", delegate { accepted.val = "confirmed"; }, delegate { accepted.val = "cancelled"; }); });
            FixtureButton("Select file", "File", delegate { SuperController.singleton.GetMediaPathDialog(delegate(string path) { chosenFile.val = path ?? ""; }, "png|jpg|jpeg", null, true); });
            FixtureButton("Internal error", "Error", delegate { SuperController.LogError("VaMMcp fixture internal-only error", false, false); });
            UIDynamicTextField holder = CreateTextField(new JSONStorableString("inputHolder", ""), true); holder.gameObject.name = "FixtureInput"; holder.height = 90; holder.UItext.enabled = false;
            GameObject child = new GameObject("FixtureEditable", typeof(RectTransform), typeof(Text)); child.transform.SetParent(holder.transform, false);
            Text text = child.GetComponent<Text>(); text.font = Font.CreateDynamicFontFromOSFont("Arial", 24); text.fontSize = 24; text.color = Color.black;
            RectTransform rect = text.rectTransform; rect.anchorMin = Vector2.zero; rect.anchorMax = Vector2.one; rect.offsetMin = new Vector2(10, 5); rect.offsetMax = new Vector2(-10, -5);
            InputField field = holder.gameObject.AddComponent<InputField>(); field.textComponent = text; field.targetGraphic = holder.backgroundImage;
            field.onValueChanged.AddListener(delegate(string value) { input.val = value; });
            field.onEndEdit.AddListener(delegate(string value) { accepted.val = value; });
        }
        private UIDynamicButton FixtureButton(string label, string id, UnityEngine.Events.UnityAction action)
        { UIDynamicButton b = CreateButton(label); b.gameObject.name = "Fixture" + id; if (action != null) b.button.onClick.AddListener(action); return b; }
        private void Increment() { count.val = count.val + 1; }
    }
}
