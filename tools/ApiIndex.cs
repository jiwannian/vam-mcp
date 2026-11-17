using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Web.Script.Serialization;
using Mono.Cecil;

class ApiIndex
{
    static string cecil;
    static void Main(string[] args)
    {
        try
        {
            cecil = args[1];
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs e) {
                return e.Name.StartsWith("Mono.Cecil,") ? Assembly.LoadFrom(cecil) : null;
            };
            Generate(args[0]);
        }
        catch (Exception error) { Console.Error.WriteLine(error); Environment.ExitCode = 1; }
    }
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static void Generate(string path)
    {
        using (AssemblyDefinition assembly = AssemblyDefinition.ReadAssembly(path))
        {
            List<object> types = new List<object>();
            foreach (TypeDefinition type in assembly.MainModule.Types) Add(type, types);
            JavaScriptSerializer json = new JavaScriptSerializer(); json.MaxJsonLength = Int32.MaxValue;
            Console.Write(json.Serialize(new Dictionary<string, object> { { "types", types } }));
        }
    }
    static void Add(TypeDefinition type, List<object> types)
    {
        if (type.IsPublic || type.IsNestedPublic)
        {
            List<string> methods = new List<string>(), fields = new List<string>(), properties = new List<string>();
            foreach (MethodDefinition m in type.Methods)
            {
                if (!m.IsPublic || m.IsGetter || m.IsSetter || m.IsAddOn || m.IsRemoveOn) continue;
                List<string> p = new List<string>(); foreach (ParameterDefinition a in m.Parameters) p.Add(a.ParameterType.FullName + " " + a.Name + (a.IsOptional ? " = " + (a.Constant == null ? "null" : a.Constant.ToString()) : ""));
                methods.Add((m.IsStatic ? "static " : "") + m.ReturnType.FullName + " " + m.Name + "(" + String.Join(", ", p.ToArray()) + ")");
            }
            foreach (FieldDefinition f in type.Fields) if (f.IsPublic) fields.Add((f.IsStatic ? "static " : "") + f.FieldType.FullName + " " + f.Name);
            foreach (PropertyDefinition p in type.Properties) if ((p.GetMethod != null && p.GetMethod.IsPublic) || (p.SetMethod != null && p.SetMethod.IsPublic)) properties.Add(p.PropertyType.FullName + " " + p.Name + " { " + (p.GetMethod != null && p.GetMethod.IsPublic ? "get; " : "") + (p.SetMethod != null && p.SetMethod.IsPublic ? "set; " : "") + "}");
            types.Add(new Dictionary<string, object> { { "name", type.FullName }, { "kind", type.IsEnum ? "enum" : type.IsInterface ? "interface" : type.IsValueType ? "struct" : "class" }, { "base_type", type.BaseType == null ? "" : type.BaseType.FullName }, { "methods", methods }, { "properties", properties }, { "fields", fields } });
        }
        foreach (TypeDefinition nested in type.NestedTypes) Add(nested, types);
    }
}
